"""Expense entries are paged like the client statement: page 1 holds the newest rows."""
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).parents[1]


class ExpensePagingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        base = Path(cls.tmp.name)
        env = {
            'DATABASE_PATH': str(base / 'test.db'), 'UPLOAD_DIR': str(base / 'uploads'),
            'BACKUP_DIR': str(base / 'backups'), 'SECRET_KEY': 'test-only',
        }
        with patch.dict(os.environ, env):
            spec = importlib.util.spec_from_file_location('app', ROOT / 'app.py')
            cls.module = importlib.util.module_from_spec(spec)
            sys.modules['app'] = cls.module
            spec.loader.exec_module(cls.module)

    @classmethod
    def tearDownClass(cls):
        sys.modules.pop('app', None)
        cls.tmp.cleanup()

    def test_newest_rows_come_first(self):
        page = self.module.newest_first_page
        rows = list(range(1, 24))  # 23 rows, oldest first
        self.assertEqual(page(rows, "10", 1), (list(range(14, 24)), 1, 3))
        self.assertEqual(page(rows, "10", 2), (list(range(4, 14)), 2, 3))
        self.assertEqual(page(rows, "10", 3), ([1, 2, 3], 3, 3))
        self.assertEqual(page(rows, "10", 99)[1], 3)
        self.assertEqual(page(rows, "10", 0)[1], 1)
        self.assertEqual(page(rows, "all", 2), (rows, 1, 1))
        self.assertEqual(page([], "10", 1), ([], 1, 1))

    def test_account_totals(self):
        entries = [
            {"currency": "CNY", "direction": "IN", "amount": 100.0},
            {"currency": "CNY", "direction": "OUT", "amount": 30.0},
            {"currency": "USD", "direction": "OUT", "amount": 5.0},
            {"currency": "EGP", "direction": "IN", "amount": 9.0},  # currency not enabled
        ]
        totals = self.module.expense_account_totals(entries, ["CNY", "USD"])
        self.assertEqual(totals["CNY"], {"in": 100.0, "out": 30.0, "balance": 70.0})
        self.assertEqual(totals["USD"]["balance"], -5.0)
        self.assertNotIn("EGP", totals)


if __name__ == '__main__':
    unittest.main()
