"""Statements and expense accounts show the newest rows first; older rows load while scrolling."""
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).parents[1]


class ScrollingLedgerTests(unittest.TestCase):
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
        for table in ('statement_entries', 'clients', 'expense_entries', 'expense_accounts'):
            db.execute(f"delete from {table}")
        db.execute("update users set must_change_password = 0")
        db.commit()
        user = db.execute("select id, session_version from users where username = 'boss'").fetchone()
        self.client = self.module.app.test_client()
        with self.client.session_transaction() as sess:
            sess['user_id'], sess['sv'], sess['_csrf_token'] = user['id'], user['session_version'], 'csrf'

    def add_statement(self, count):
        cur = self.db.execute("insert into clients(name) values ('Acme')")
        client_id = cur.lastrowid
        for n in range(1, count + 1):
            self.db.execute(
                "insert into statement_entries(client_id, source_no, entry_date, description, currency, direction,"
                " amount, kind, category_hint) values (?, ?, ?, ?, 'USD', 'IN', 10, 'deposit', 'uncategorized')",
                (client_id, n, f"2026-{1 + n // 28:02d}-{1 + n % 28:02d}", f"Row {n}"),
            )
        self.db.commit()
        return client_id

    # --- The pieces -------------------------------------------------------------------

    def test_chunks_walk_from_newest_to_oldest(self):
        chunk = self.module.newest_first_chunk
        rows = [{"id": n, "entry_date": f"2026-01-{n:02d}"} for n in range(1, 8)]  # oldest first
        first, more = chunk(rows, 3)
        self.assertEqual([r["id"] for r in first], [7, 6, 5])
        self.assertTrue(more)
        second, more = chunk(rows, 3, ("2026-01-05", 5))
        self.assertEqual([r["id"] for r in second], [4, 3, 2])
        self.assertTrue(more)
        last, more = chunk(rows, 3, ("2026-01-02", 2))
        self.assertEqual([r["id"] for r in last], [1])
        self.assertFalse(more)
        everything, more = chunk(rows, None)
        self.assertEqual(len(everything), 7)
        self.assertFalse(more)

    def test_a_deleted_cursor_row_still_continues_in_place(self):
        rows = [{"id": n, "entry_date": "2026-01-01"} for n in (1, 2, 4, 5)]  # row 3 was deleted
        older, _ = self.module.newest_first_chunk(rows, 10, ("2026-01-01", 3))
        self.assertEqual([r["id"] for r in older], [2, 1])

    def test_shown_count(self):
        shown = self.module.ledger_shown_count
        self.assertEqual(shown(None), self.module.LEDGER_CHUNK)
        self.assertEqual(shown("rubbish"), self.module.LEDGER_CHUNK)
        self.assertEqual(shown("90"), 90)
        self.assertIsNone(shown("all"))
        self.assertEqual(shown("10", 3), 30)  # old ?per_page=10&page=3 links open with those rows
        self.assertEqual(shown("999999"), self.module.LEDGER_MAX_ROWS)

    # --- Client statement ---------------------------------------------------------------

    def test_statement_opens_newest_first_and_loads_the_rest(self):
        client_id = self.add_statement(75)
        page = self.client.get(f'/clients/{client_id}').get_data(as_text=True)
        newest, next_newest = [r['id'] for r in self.db.execute(
            "select id from statement_entries order by entry_date desc, id desc limit 2")]
        self.assertLess(page.index(f'id="entry-{newest}"'), page.index(f'id="entry-{next_newest}"'))
        self.assertEqual(page.count('class="display-row"'), self.module.LEDGER_CHUNK)
        self.assertIn('data-has-more="1"', page)

        seen = [r['id'] for r in self.db.execute(
            "select id from statement_entries order by entry_date desc, id desc limit ?", (self.module.LEDGER_CHUNK,))]
        oldest = self.db.execute("select entry_date, id from statement_entries where id = ?", (seen[-1],)).fetchone()
        cursor = {'before_date': oldest['entry_date'], 'before_id': oldest['id']}
        while True:
            data = self.client.get(f'/clients/{client_id}/rows', query_string={**cursor, 'limit': 30}).get_json()
            self.assertTrue(data['ok'])
            self.assertEqual(data['total'], 75)
            self.assertEqual(data['rows_html'].count('class="display-row"'), data['count'])
            self.assertEqual(data['cards_html'].count('class="mobile-entry-card"'), data['count'])
            seen += [int(part.split('"')[0]) for part in data['rows_html'].split('id="entry-')[1:]]
            if not data['has_more']:
                break
            cursor = {'before_date': data['before_date'], 'before_id': data['before_id']}
        self.assertEqual(sorted(seen), sorted(r['id'] for r in self.db.execute("select id from statement_entries")))
        self.assertEqual(len(seen), len(set(seen)))

    def test_statement_keeps_the_rows_a_form_came_back_with(self):
        client_id = self.add_statement(75)
        page = self.client.get(f'/clients/{client_id}?per_page=60').get_data(as_text=True)
        self.assertEqual(page.count('class="display-row"'), 60)
        page = self.client.get(f'/clients/{client_id}?per_page=all').get_data(as_text=True)
        self.assertEqual(page.count('class="display-row"'), 75)
        self.assertNotIn('data-has-more="1"', page)

    def test_statement_rows_follow_filters_and_view(self):
        client_id = self.add_statement(40)
        data = self.client.get(f'/clients/{client_id}/rows', query_string={'q': 'Row 3', 'view': 'split', 'limit': 'all'}).get_json()
        self.assertEqual(data['total'], 11)  # Row 3, Row 30..39
        self.assertIn('USD', data['rows_html'])
        self.assertFalse(data['has_more'])

    def test_statement_balances_endpoint(self):
        client_id = self.add_statement(3)
        data = self.client.get(f'/clients/{client_id}/balances').get_json()
        self.assertEqual(sorted(v['usd'] for v in data['balances'].values()), [10.0, 20.0, 30.0])
        self.assertEqual(self.client.get('/clients/999999/rows').status_code, 404)

    def test_commission_rows_show_company_status_instead_of_commission_button(self):
        client_id = self.add_statement(0)
        account_id = self.db.execute("insert into expense_accounts(name, enabled_currencies) values ('Office', 'USD')").lastrowid
        rows = {}
        for name, category, profit in (('Factory', 'factory_payment', None), ('Fee in', 'commission', account_id), ('Fee out', 'commission', None)):
            rows[name] = self.db.execute(
                "insert into statement_entries(client_id, source_no, entry_date, description, currency, direction,"
                " amount, kind, category_hint, profit_expense_account_id) values (?, 0, '2026-05-01', ?, 'USD', 'OUT', 10, 'movement', ?, ?)",
                (client_id, name, category, profit),
            ).lastrowid
        self.db.commit()
        page = self.client.get(f'/clients/{client_id}').get_data(as_text=True)

        def row_html(entry_id):
            return page.split(f'id="entry-{entry_id}"')[1].split('</tr>')[0]

        self.assertIn('data-commission-open', row_html(rows['Factory']))
        self.assertNotIn('data-commission-open', row_html(rows['Fee in']))
        self.assertIn('Added to company profit (Office)', row_html(rows['Fee in']))
        self.assertNotIn('data-commission-open', row_html(rows['Fee out']))
        self.assertIn('Not added to company profit', row_html(rows['Fee out']))
        response = self.client.post(f'/clients/{client_id}/commission', data={
            'csrf_token': 'csrf', 'source_entry_id': rows['Fee out'], 'commission_date': '2026-05-02', 'percentage': '5'})
        self.assertEqual(response.status_code, 302)
        self.assertIsNone(self.db.execute(
            "select id from statement_entries where commission_source_entry_id = ?", (rows['Fee out'],)).fetchone())

    def test_undo_says_what_it_undoes_and_needs_the_confirmed_change(self):
        client_id = self.add_statement(0)
        self.client.post(f'/clients/{client_id}/entries', data={
            'csrf_token': 'csrf', 'entry_date': '2026-06-01', 'description': 'Freight', 'currency': 'USD',
            'direction': 'OUT', 'amount': '120', 'kind': 'movement', 'category_hint': 'shipping_expense'})
        entry_id = self.db.execute("select id from statement_entries where client_id = ?", (client_id,)).fetchone()['id']
        self.client.post(f'/entries/{entry_id}/save', data={
            'csrf_token': 'csrf', 'entry_id': entry_id, 'entry_date': '2026-06-01', 'description': 'Freight',
            'currency': 'USD', 'direction': 'OUT', 'amount': '150', 'kind': 'movement', 'category_hint': 'shipping_expense'})
        page = self.client.get(f'/clients/{client_id}').get_data(as_text=True)
        self.assertIn('Edited an entry', page)
        self.assertIn('<s>$150.00</s> &rarr; <strong>$120.00</strong>', page)
        event_id = int(page.split('name="event_id" value="')[1].split('"')[0])

        # A stale or missing confirmation undoes nothing.
        self.client.post(f'/clients/{client_id}/undo', data={'csrf_token': 'csrf', 'event_id': event_id - 1})
        self.client.post(f'/clients/{client_id}/undo', data={'csrf_token': 'csrf'})
        amount = lambda: self.db.execute("select amount from statement_entries where id = ?", (entry_id,)).fetchone()['amount']
        self.assertEqual(amount(), 150)

        self.client.post(f'/clients/{client_id}/undo', data={'csrf_token': 'csrf', 'event_id': event_id})
        self.assertEqual(amount(), 120)
        page = self.client.get(f'/clients/{client_id}').get_data(as_text=True)
        self.assertIn('Added an entry', page)
        self.assertIn('Undo deletes this entry again.', page)

    def test_only_listed_categories_are_saved(self):
        # Bots once saved categories the app never offered ("accommodation"), and editing such an
        # entry then failed with "Bad Request". Now every way in refuses them and a cleanup moves old ones.
        client_id = self.add_statement(0)
        insert = ("insert into statement_entries(client_id, source_no, entry_date, description, currency, direction,"
                  " amount, kind, category_hint) values (?, ?, '2023-05-15', ?, 'CNY', 'OUT', 300, 'movement', ?)")
        ids = {cat: self.db.execute(insert, (client_id, n, cat, cat)).lastrowid
               for n, cat in enumerate(('accommodation', 'Transport', 'deposit', 'something odd'), start=1)}
        self.db.commit()
        category = lambda entry_id: self.db.execute(
            "select category_hint from statement_entries where id = ?", (entry_id,)).fetchone()['category_hint']

        # The edit form sends no category for such an entry: a clear message, not a 400, and nothing changes.
        form = {'csrf_token': 'csrf', 'entry_date': '2026-05-15', 'description': 'Lifeng Hotel',
                'currency': 'CNY', 'direction': 'OUT', 'amount': '300', 'kind': 'movement'}
        response = self.client.post(f"/entries/{ids['accommodation']}/save", data=form)
        self.assertEqual(response.status_code, 302)
        self.assertIn('Unknown category (none)', self.client.get(f'/clients/{client_id}').get_data(as_text=True))
        self.assertEqual(category(ids['accommodation']), 'accommodation')
        response = self.client.post(f"/entries/{ids['accommodation']}/save", data={**form, 'category_hint': 'travel_expense'})
        self.assertEqual(response.status_code, 302)
        row = self.db.execute("select entry_date, category_hint from statement_entries where id = ?",
                              (ids['accommodation'],)).fetchone()
        self.assertEqual((row['entry_date'], row['category_hint']), ('2026-05-15', 'travel_expense'))

        # The cleanup that runs when the app starts.
        self.assertEqual(self.module.move_old_categories(self.db), 3)
        self.db.commit()
        self.assertEqual(category(ids['Transport']), 'travel_expense')
        self.assertEqual(category(ids['deposit']), 'client_receipt')
        self.assertEqual(category(ids['something odd']), 'uncategorized')
        self.assertEqual(self.module.move_old_categories(self.db), 0)

        # The API refuses unknown categories and says which ones it takes.
        from hashlib import sha256
        raw = 'ffs_' + 'c' * 64
        self.db.execute("insert into api_tokens(name, token_hash, token_prefix, access_level, created_at)"
                        " values ('bot', ?, 'ffs_c', 'full_control', '2026')", (sha256(raw.encode()).hexdigest(),))
        self.db.commit()
        api = self.module.app.test_client()
        auth = {'Authorization': f'Bearer {raw}'}
        entry = {'entry_date': '2026-06-01', 'description': 'Hotel', 'currency': 'CNY', 'direction': 'OUT', 'amount': 200}
        refused = api.post(f'/api/v1/clients/{client_id}/entries', headers=auth, json={**entry, 'category_hint': 'hotel'})
        self.assertEqual(refused.status_code, 400)
        self.assertIn('travel_expense', refused.get_json()['error'])
        made = api.post(f'/api/v1/clients/{client_id}/entries', headers=auth, json=entry)
        self.assertEqual(made.status_code, 201)
        self.assertEqual(made.get_json()['entry']['category_hint'], 'uncategorized')
        entry_id = made.get_json()['entry']['id']
        self.assertEqual(api.patch(f'/api/v1/entries/{entry_id}', headers=auth, json={'category_hint': 'transport'}).status_code, 400)
        self.assertEqual(api.patch(f'/api/v1/entries/{entry_id}', headers=auth, json={'category_hint': 'travel_expense'}).status_code, 200)
        self.assertEqual(category(entry_id), 'travel_expense')

    # --- Expense account ----------------------------------------------------------------

    def test_expense_account_scrolls_the_same_way(self):
        account_id = self.db.execute("insert into expense_accounts(name, enabled_currencies) values ('Office', 'CNY')").lastrowid
        for n in range(1, 46):
            self.db.execute(
                "insert into expense_entries(account_id, seq_no, entry_date, description, currency, direction, amount)"
                " values (?, ?, ?, ?, 'CNY', 'OUT', 5)",
                (account_id, n, f"2026-03-{1 + n % 28:02d}", f"Cost {n}"),
            )
        self.db.commit()
        page = self.client.get(f'/expenses/accounts/{account_id}').get_data(as_text=True)
        self.assertEqual(page.count('class="display-row"'), self.module.LEDGER_CHUNK)
        before = page.split('data-before="')[1].split('"')[0].split('|')
        data = self.client.get(f'/expenses/accounts/{account_id}/rows',
                               query_string={'before_date': before[0], 'before_id': before[1]}).get_json()
        self.assertEqual(data['count'], 15)
        self.assertFalse(data['has_more'])
        self.assertEqual(data['rows_html'].count('class="display-row"'), 15)


if __name__ == '__main__':
    unittest.main()
