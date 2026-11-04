"""Statement and expense exports: totals, date range, who owes whom, the overall balance,
the same column names in Excel and PDF, exchange pairs, filters and print setup."""
import importlib.util
import io
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import openpyxl

ROOT = Path(__file__).parents[1]
HEADERS = ['Date', 'No.', 'Description', 'USD In', 'USD Out', 'RMB In', 'RMB Out', 'USD Balance', 'RMB Balance', 'Rate']


class BetterExportTests(unittest.TestCase):
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
        for table in ('statement_entries', 'clients', 'client_orders', 'expense_entries', 'expense_accounts'):
            db.execute(f"delete from {table}")
        db.execute("delete from app_settings where key like 'fx_%'")
        db.execute("update users set must_change_password = 0")
        self.module.save_manual_fx_rate(db, 7.15)
        self.client_id = db.execute("insert into clients(name) values ('Sample Trading Co')").lastrowid
        for no, (day, desc, currency, direction, amount, rate, group) in enumerate([
            ('2026-08-02', 'Deposit', 'USD', 'IN', 10000, None, None),
            ('2026-08-04', 'Exchange USD to RMB', 'USD', 'OUT', 4000, 7.0887, 'g1'),
            ('2026-08-04', 'Exchange USD to RMB', 'CNY', 'IN', 28354.8, 7.0887, 'g1'),
            ('2026-08-09', 'Paid supplier', 'CNY', 'OUT', 100000, None, None),
        ], start=1):
            db.execute(
                "insert into statement_entries(client_id, source_no, entry_date, description, currency, direction,"
                " amount, kind, category_hint, exchange_rate, transfer_group) values (?, ?, ?, ?, ?, ?, ?, ?, 'uncategorized', ?, ?)",
                (self.client_id, no, day, desc, currency, direction, amount, 'exchange' if group else 'movement', rate, group),
            )
        self.account_id = db.execute(
            "insert into expense_accounts(name, enabled_currencies) values ('Firefly Trading', 'CNY')"
        ).lastrowid
        for no, (day, desc, direction, amount, category) in enumerate([
            ('2026-10-06', 'Commission', 'IN', 500, 'general'),
            ('2026-10-09', 'Office rent', 'OUT', 3000, 'rent'),
        ], start=1):
            db.execute(
                "insert into expense_entries(account_id, seq_no, entry_date, description, currency, direction, amount,"
                " category, is_recurring) values (?, ?, ?, ?, 'CNY', ?, ?, ?, 0)",
                (self.account_id, no, day, desc, direction, amount, category),
            )
        db.commit()
        user = db.execute("select id, session_version from users where username = 'boss'").fetchone()
        self.web = self.module.app.test_client()
        with self.web.session_transaction() as sess:
            sess['user_id'], sess['sv'], sess['_csrf_token'] = user['id'], user['session_version'], 'csrf'

    def statement_rows(self):
        return self.module._statement_export_data(self.db, self.client_id, 'all')[1]

    def sheet(self, path):
        response = self.web.get(path)
        self.assertEqual(response.status_code, 200)
        return openpyxl.load_workbook(io.BytesIO(response.data)).active

    def test_summary_says_who_owes_whom_and_the_overall_balance(self):
        summary = self.module.statement_export_summary(self.statement_rows())
        self.assertEqual((summary['date_from'], summary['date_to']), ('2 Aug 2026', '9 Aug 2026'))
        self.assertEqual(summary['totals']['USD'], {'IN': 10000, 'OUT': 4000})
        self.assertEqual(summary['usd_note'], 'we hold this for you')  # $6,000
        self.assertEqual(summary['cny_note'], 'you owe us')  # -¥71,645.20
        # 6000 - 71645.20 / 7.15 = -4020.31
        self.assertEqual(summary['overall']['usd'], -4020.31)
        self.assertEqual(summary['overall']['text'], 'you owe us $4,020.31')
        self.assertEqual(summary['overall']['rate'], 7.15)

    def test_no_overall_line_with_only_one_currency_or_no_rate(self):
        rows = [row for row in self.statement_rows() if row['currency'] == 'USD' and not row['transfer_group']]
        self.assertIsNone(self.module.statement_export_summary(rows)['overall'])
        self.db.execute("delete from app_settings where key like 'fx_%'")
        self.db.execute("update statement_entries set exchange_rate = null")
        self.db.commit()
        self.assertIsNone(self.module.statement_export_summary(self.statement_rows())['overall'])

    def test_the_two_lines_of_an_exchange_are_marked_as_a_pair(self):
        pairs = [row['pair'] for row in self.module.mark_exchange_pairs(self.statement_rows())]
        self.assertEqual(pairs, ['', 'first', 'second', ''])

    def test_statement_excel(self):
        ws = self.sheet(f'/clients/{self.client_id}/export.xlsx')
        values = [[c for c in row if c is not None] for row in ws.iter_rows(values_only=True)]
        self.assertEqual(values[1], ['Statement · 2 Aug 2026 – 9 Aug 2026 · 4 entries'])
        self.assertEqual(values[2], ['USD: $6,000.00 — we hold this for you'])
        self.assertEqual(values[3], ['RMB: ¥71,645.20 — you owe us'])
        self.assertTrue(values[4][0].startswith('Overall: you owe us $4,020.31 (RMB converted at 7.1500'))
        header_row = values.index(HEADERS) + 1
        self.assertEqual(ws.auto_filter.ref, f'A{header_row}:J{header_row + 4}')
        self.assertEqual(ws.freeze_panes, f'A{header_row + 1}')
        self.assertEqual(ws.print_title_rows, f'${header_row}:${header_row}')
        self.assertEqual(ws.page_setup.orientation, 'landscape')
        self.assertTrue(ws.sheet_properties.pageSetUpPr.fitToPage)
        first = header_row + 1
        self.assertEqual(ws.cell(row=first + 1, column=10).value, 7.0887)  # one Rate column
        totals = [ws.cell(row=first + 4, column=c).value for c in range(1, 10)]
        self.assertEqual(totals[0], 'Totals')
        self.assertEqual(totals[3:], [f'=SUM(D{first}:D{first + 3})', f'=SUM(E{first}:E{first + 3})',
                                      f'=SUM(F{first}:F{first + 3})', f'=SUM(G{first}:G{first + 3})',
                                      f'=D{first + 4}-E{first + 4}', f'=F{first + 4}-G{first + 4}'])
        self.assertIn('[Red]', ws.cell(row=first, column=9).number_format)

    def test_expense_excel_has_signs_red_negatives_and_totals(self):
        ws = self.sheet(f'/expenses/accounts/{self.account_id}/export.xlsx')
        values = [[c for c in row if c is not None] for row in ws.iter_rows(values_only=True)]
        self.assertEqual(values[1], ['Expense report · 6 Oct 2026 – 9 Oct 2026 · 2 entries'])
        self.assertIn(['Date', 'No.', 'Description', 'Category', 'RMB In', 'RMB Out', 'RMB Balance'], values)
        self.assertEqual(values[-1], ['Totals', '=SUM(E5:E6)', '=SUM(F5:F6)', '=E7-F7'])
        self.assertEqual(ws['G6'].value, -2500)
        self.assertEqual(ws['G6'].number_format, '"¥"#,##0.00;[Red]-"¥"#,##0.00')
        self.assertEqual(ws.auto_filter.ref, 'A4:G6')

    def test_pdf_page_shows_the_new_lines(self):
        rows = self.statement_rows()
        summary = self.module.statement_export_summary(rows)
        client = {'name': 'Sample Trading Co'}
        with self.module.app.test_request_context():
            html = self.module.render_template(
                'pdf.html', brand=self.module.pdf_branding(), client=client, rows=self.module.mark_exchange_pairs(rows),
                generated_date='2026-11-05', final_usd=summary['final_usd'], final_cny=summary['final_cny'], summary=summary)
        for text in ('2 Aug 2026 &ndash; 9 Aug 2026', 'we hold this for you', 'you owe us $4,020.31',
                     'RMB converted at 7.1500', 'RMB Balance', 'class="ex ex-second"', 'Totals', '$10,000.00'):
            self.assertIn(text, html)
        self.assertNotIn('CNY Bal', html)

    def test_pdf_renders(self):
        if self.module.weasyprint is None:
            self.skipTest('PDF library not installed here')
        response = self.web.get(f'/clients/{self.client_id}/export.pdf')
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data.startswith(b'%PDF'))


if __name__ == '__main__':
    unittest.main()
