"""Orders inside a client statement: closing, carrying the balance over, reopening and editing."""
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).parents[1]


class ClientOrderTests(unittest.TestCase):
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
        for table in ('statement_entries', 'clients', 'client_orders'):
            db.execute(f"delete from {table}")
        db.execute("delete from users where username = 'staff'")
        db.execute("update users set must_change_password = 0")
        db.execute(
            "insert into users(username, password_hash, role, created_at) values ('staff', 'x', 'user', '')"
        )
        self.client_id = db.execute("insert into clients(name) values ('Cairo Home Goods')").lastrowid
        db.commit()
        self.add_raw('2026-03-01', 'Payment in', 'USD', 'IN', 1000)
        self.add_raw('2026-03-05', 'Mold supplier', 'USD', 'OUT', 600)
        self.add_raw('2026-03-06', 'Deposit', 'CNY', 'IN', 500)
        self.admin = self.login('boss')
        self.staff = self.login('staff')

    def login(self, username):
        user = self.db.execute("select id, session_version from users where username = ?", (username,)).fetchone()
        client = self.module.app.test_client()
        with client.session_transaction() as sess:
            sess['user_id'], sess['sv'], sess['_csrf_token'] = user['id'], user['session_version'], 'csrf'
        return client

    def add_raw(self, day, desc, currency, direction, amount):
        """An entry saved the way the API and bots save one: without saying which order."""
        self.db.execute(
            "insert into statement_entries(client_id, source_no, entry_date, description, currency, direction,"
            " amount, kind, category_hint) values (?, 0, ?, ?, ?, ?, ?, 'movement', 'uncategorized')",
            (self.client_id, day, desc, currency, direction, amount),
        )
        self.db.commit()

    def orders(self):
        return self.module.client_orders(self.db, self.client_id)

    def order_of(self, desc):
        return self.db.execute("select order_id from statement_entries where description = ?", (desc,)).fetchone()[0]

    def post(self, who, path, **data):
        return who.post(path, data={'csrf_token': 'csrf', **data})

    def test_new_clients_and_entries_start_in_order_1(self):
        orders = self.orders()
        self.assertEqual([(o['name'], o['is_open'], o['entry_count']) for o in orders], [('Order 1', True, 3)])

    def test_closing_starts_the_next_order_and_carries_the_balance(self):
        first = self.orders()[0]
        response = self.post(self.staff, f'/clients/{self.client_id}/orders/{first["id"]}/close', closed_at='2026-03-10')
        self.assertEqual(response.status_code, 302)
        first, second = self.orders()
        self.assertEqual((first['closed_at'], second['name'], second['is_open']), ('2026-03-10', 'Order 2', True))
        self.add_raw('2026-03-12', 'Next order payment', 'USD', 'OUT', 100)
        self.assertEqual(self.order_of('Next order payment'), second['id'])
        self.assertEqual(self.module.order_opening_balance(self.db, second), (400.0, 500.0))

        page = self.admin.get(f'/clients/{self.client_id}?order={second["id"]}').get_data(as_text=True)
        self.assertIn('Brought forward', page)
        self.assertIn('Next order payment', page)
        self.assertNotIn('Mold supplier', page)
        # The balance at the end of Order 2 is still the client's whole balance.
        self.assertIn('$300.00', page)
        balances = self.admin.get(f'/clients/{self.client_id}/balances?order={second["id"]}').get_json()['balances']
        self.assertEqual(list(balances.values())[0], {'usd': 300.0, 'cny': 500.0})
        everything = self.admin.get(f'/clients/{self.client_id}?order=all').get_data(as_text=True)
        self.assertIn('Mold supplier', everything)
        self.assertIn('Next order payment', everything)

    def test_closed_orders_are_for_admins_only(self):
        first = self.orders()[0]
        self.post(self.staff, f'/clients/{self.client_id}/orders/{first["id"]}/close', closed_at='2026-03-10')
        entry_id = self.db.execute("select id from statement_entries where description = 'Mold supplier'").fetchone()[0]
        form = dict(entry_date='2026-03-05', description='Changed', amount='600', currency='USD', direction='OUT',
                    kind='movement', category_hint='uncategorized')
        self.post(self.staff, f'/entries/{entry_id}/save', **form)
        self.assertEqual(self.order_of('Mold supplier'), first['id'])  # not changed
        self.post(self.staff, f'/entries/{entry_id}/delete')
        self.assertIsNotNone(self.order_of('Mold supplier'))  # not deleted
        self.post(self.staff, f'/clients/{self.client_id}/entries', order_id=first['id'], submission_token='',
                  **{**form, 'description': 'Late add'})
        self.assertIsNone(self.db.execute("select 1 from statement_entries where description = 'Late add'").fetchone())
        self.assertEqual(self.post(self.staff, f'/clients/{self.client_id}/orders/{first["id"]}/reopen').status_code, 403)
        staff_page = self.staff.get(f'/clients/{self.client_id}?order={first["id"]}').get_data(as_text=True)
        self.assertIn('is-order-locked', staff_page)
        self.assertIn('Only an admin can change it', staff_page)
        self.assertNotIn('is-order-locked', self.admin.get(f'/clients/{self.client_id}?order={first["id"]}').get_data(as_text=True))

        self.post(self.admin, f'/entries/{entry_id}/save', **form)
        self.assertEqual(self.db.execute("select description from statement_entries where id = ?", (entry_id,)).fetchone()[0], 'Changed')
        self.post(self.admin, f'/clients/{self.client_id}/entries', order_id=first['id'], submission_token='',
                  **{**form, 'description': 'Late add'})
        self.assertEqual(self.order_of('Late add'), first['id'])

    def test_reopen_removes_the_empty_order_it_started(self):
        first = self.orders()[0]
        self.post(self.admin, f'/clients/{self.client_id}/orders/{first["id"]}/close', closed_at='2026-03-10')
        self.assertEqual(len(self.orders()), 2)
        self.post(self.admin, f'/clients/{self.client_id}/orders/{first["id"]}/reopen')
        orders = self.orders()
        self.assertEqual([(o['name'], o['is_open']) for o in orders], [('Order 1', True)])

    def test_rename_and_move_the_closing_date(self):
        first = self.orders()[0]
        self.post(self.admin, f'/clients/{self.client_id}/orders/{first["id"]}/close', closed_at='2026-03-10')
        second = self.orders()[1]
        self.post(self.admin, f'/clients/{self.client_id}/orders/{first["id"]}/edit', name='  March   container ', closed_at='2026-03-04')
        first = self.orders()[0]
        self.assertEqual((first['name'], first['closed_at']), ('March container', '2026-03-04'))
        # Entries after the earlier closing date moved to the next order...
        self.assertEqual(self.order_of('Mold supplier'), second['id'])
        self.assertEqual(self.order_of('Payment in'), first['id'])
        # ...and come back when the date moves later again.
        self.post(self.admin, f'/clients/{self.client_id}/orders/{first["id"]}/edit', name='March container', closed_at='2026-03-10')
        self.assertEqual(self.order_of('Mold supplier'), first['id'])

    def test_exports_follow_the_order(self):
        first = self.orders()[0]
        self.post(self.admin, f'/clients/{self.client_id}/orders/{first["id"]}/close', closed_at='2026-03-10')
        second = self.orders()[1]
        self.add_raw('2026-03-12', 'Next order payment', 'USD', 'OUT', 100)
        client, rows = self.module._statement_export_data(self.db, self.client_id, str(second['id']))
        self.assertEqual(client['name'], 'Cairo Home Goods - Order 2')
        self.assertEqual([r['description'] for r in rows][-1], 'Next order payment')
        self.assertTrue(rows[0]['is_carried_balance'])
        self.assertEqual((rows[-1]['running_usd'], rows[-1]['running_cny']), (300.0, 500.0))
        _, all_rows = self.module._statement_export_data(self.db, self.client_id)
        self.assertEqual(len(all_rows), 4)
        csv_text = self.admin.get(f'/clients/{self.client_id}/export.csv?order={second["id"]}').get_data(as_text=True)
        self.assertIn('Next order payment', csv_text)
        self.assertNotIn('Mold supplier', csv_text)
        self.assertEqual(self.admin.get(f'/clients/{self.client_id}/export.xlsx?order={second["id"]}').status_code, 200)

    def test_undo_brings_a_deleted_entry_back_to_its_order(self):
        first = self.orders()[0]
        self.post(self.admin, f'/clients/{self.client_id}/orders/{first["id"]}/close', closed_at='2026-03-10')
        entry_id = self.db.execute("select id from statement_entries where description = 'Mold supplier'").fetchone()[0]
        self.post(self.admin, f'/entries/{entry_id}/delete')
        event = self.module.latest_undo_event(self.client_id)
        self.post(self.admin, f'/clients/{self.client_id}/undo', event_id=event['id'])
        self.assertEqual(self.order_of('Mold supplier'), first['id'])

    def test_older_databases_get_order_1(self):
        self.db.execute("delete from client_orders")
        self.db.execute("update statement_entries set order_id = null")
        self.module.setup_client_orders(self.db)
        orders = self.orders()
        self.assertEqual([(o['name'], o['entry_count']) for o in orders], [('Order 1', 3)])


if __name__ == '__main__':
    unittest.main()
