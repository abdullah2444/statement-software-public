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
