"""Dashboard: search that starts with a client's name, one-field New client, and the new layout."""
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).parents[1]


class DashboardSearchTests(unittest.TestCase):
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
        for table in ('statement_entries', 'clients'):
            db.execute(f"delete from {table}")
        db.execute("update users set must_change_password = 0")
        db.commit()
        self.ids = {}
        for name in ('Golden Gate Imports', 'Golden Gate - Branch 2', 'Cairo Home Goods'):
            self.ids[name] = db.execute("insert into clients(name) values (?)", (name,)).lastrowid
            for n, desc in enumerate(('Freight to port', 'Factory payment'), start=1):
                db.execute(
                    "insert into statement_entries(client_id, source_no, entry_date, description, currency, direction,"
                    " amount, kind, category_hint) values (?, ?, '2026-05-0' || ?, ?, 'USD', 'OUT', 100, 'movement', 'uncategorized')",
                    (self.ids[name], n, n, desc),
                )
        db.commit()
        user = db.execute("select id, session_version from users where username = 'boss'").fetchone()
        self.client = self.module.app.test_client()
        with self.client.session_transaction() as sess:
            sess['user_id'], sess['sv'], sess['_csrf_token'] = user['id'], user['session_version'], 'csrf'

    def search(self, q, **extra):
        return self.client.get('/api/global-search', query_string={'q': q, **extra}).get_json()

    def test_client_name_first_then_the_search(self):
        split = lambda q: self.module.split_client_prefix(self.db, q)
        client, term = split('golden gate imports freight')
        self.assertEqual((client['name'], term), ('Golden Gate Imports', 'freight'))
        client, term = split('Golden Gate - Branch 2 factory')  # the longest name wins
        self.assertEqual((client['name'], term), ('Golden Gate - Branch 2', 'factory'))
        client, term = split('cairo freight')  # the start of a name is enough when only one client fits
        self.assertEqual((client['name'], term), ('Cairo Home Goods', 'freight'))
        client, term = split('golden freight')  # two clients start with "golden": no guessing
        self.assertIsNone(client)
        self.assertEqual(term, 'golden freight')

    def test_search_inside_one_client(self):
        data = self.search('Cairo Home Goods freight')
        self.assertEqual(data['client']['name'], 'Cairo Home Goods')
        self.assertEqual(data['term'], 'freight')
        self.assertEqual({e['client_name'] for e in data['entries']}, {'Cairo Home Goods'})
        self.assertEqual([e['description'] for e in data['entries']], ['Freight to port'])

        # A picked client (client_id) searches only there; with no words it lists the latest entries.
        data = self.search('', client_id=self.ids['Golden Gate Imports'])
        self.assertEqual(len(data['entries']), 2)
        self.assertEqual({e['client_id'] for e in data['entries']}, {self.ids['Golden Gate Imports']})

    def test_search_everywhere_and_client_suggestions(self):
        data = self.search('freight')
        self.assertIsNone(data['client'])
        self.assertEqual(len(data['entries']), 3)
        data = self.search('golden gate samples')
        self.assertEqual({c['name'] for c in data['clients']}, {'Golden Gate Imports', 'Golden Gate - Branch 2'})
        self.assertEqual(self.search('g')['entries'], [])

    def test_new_client_needs_only_a_name_and_refuses_a_duplicate(self):
        response = self.client.post('/clients/new', data={'csrf_token': 'csrf', 'name': '  Lagos   Fabrics '})
        new_id = self.db.execute("select id from clients where name = 'Lagos Fabrics'").fetchone()['id']
        self.assertTrue(response.headers['Location'].endswith(f'/clients/{new_id}'))

        response = self.client.post('/clients/new', data={'csrf_token': 'csrf', 'name': 'lagos fabrics'})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers['Location'].endswith('/#clients'))
        self.assertEqual(self.db.execute("select count(*) from clients where name like 'lagos fabrics'").fetchone()[0], 1)
        page = self.client.get('/').get_data(as_text=True)
        self.assertIn('There is already a client called &#34;Lagos Fabrics&#34;.', page)

    def test_dashboard_layout(self):
        page = self.client.get('/').get_data(as_text=True)
        for text in ('Company position', 'Bank accounts', 'Suppliers', 'Expense accounts', 'data-new-client',
                     'id="client-search"', 'Golden Gate - Branch 2'):
            self.assertIn(text, page)
        self.assertIn('-$200.00', page)  # minus before the symbol, never "$-200.00"
        self.assertNotIn('$-', page)


if __name__ == '__main__':
    unittest.main()
