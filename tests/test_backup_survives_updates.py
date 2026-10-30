"""Cloud backup across updates: the server's id and restic's cache are kept on the
server, and a file Backblaze keeps refusing stops restic quickly with a clear reason."""
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT))

import cloud_backup  # noqa: E402


class KeptAcrossUpdatesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        base = Path(cls.tmp.name)
        (base / 'data').mkdir()
        cls.env = {
            'DATABASE_PATH': str(base / 'data' / 'test.db'), 'UPLOAD_DIR': str(base / 'data' / 'uploads'),
            'BACKUP_DIR': str(base / 'data' / 'backups'), 'UPDATER_DIR': str(base / 'data' / 'updater'),
            'SECRET_KEY': 'test-only', 'INITIAL_ADMIN_USERNAME': 'boss', 'INITIAL_ADMIN_PASSWORD': 'boss-test-pass',
        }
        with patch.dict(os.environ, cls.env):
            spec = importlib.util.spec_from_file_location('app', ROOT / 'app.py')
            cls.module = importlib.util.module_from_spec(spec)
            sys.modules['app'] = cls.module
            spec.loader.exec_module(cls.module)
            cls.module.init_db()

    @classmethod
    def tearDownClass(cls):
        sys.modules.pop('app', None)
        cls.tmp.cleanup()

    def setUp(self):
        m = self.module
        for path in (m.INSTANCE_ID_FILE, m.OLD_INSTANCE_ID_FILE):
            path.unlink(missing_ok=True)
        self.updater = m.UPDATER_DIR
        (self.updater / 'logs').mkdir(parents=True, exist_ok=True)
        for path in [self.updater / 'status.json', *(self.updater / 'logs').glob('*')]:
            path.unlink(missing_ok=True)
        self.set_owner('')

    def set_owner(self, value):
        with self.module.app.app_context():
            self.module.set_setting('cloud_backup_instance', value)

    def finished_update(self, target, log_lines):
        run_id = '20261030T150000Z-abc123'
        (self.updater / 'status.json').write_text(json.dumps({'id': run_id, 'target': target, 'state': 'running'}))
        (self.updater / 'logs' / f'{run_id}.log').write_text('\n'.join(log_lines) + '\n', encoding='utf-8')

    def test_id_is_kept_in_the_backups_folder(self):
        m = self.module
        first = m.this_instance_id()
        self.assertEqual(m.INSTANCE_ID_FILE.parent, m.BACKUP_DIR)
        self.assertEqual(m.INSTANCE_ID_FILE.read_text().strip(), first)
        self.assertEqual(m.this_instance_id(), first)
        # The backups list only shows backup files, not this or restic's cache.
        (m.BACKUP_DIR / '.restic-cache').mkdir(exist_ok=True)
        with m.app.app_context():
            names = [item['name'] for item in m.list_database_backups()]
        self.assertNotIn('.instance-id', names)
        self.assertNotIn('.restic-cache', names)

    def test_id_from_before_5_2_22_is_moved(self):
        m = self.module
        m.OLD_INSTANCE_ID_FILE.write_text('0123456789ab')
        self.assertEqual(m.this_instance_id(), '0123456789ab')
        self.assertEqual(m.INSTANCE_ID_FILE.read_text().strip(), '0123456789ab')

    def test_owner_keeps_backing_up_after_the_update_that_moves_the_id(self):
        m = self.module
        self.set_owner('8935cefe7bcb')
        self.finished_update('v' + m.APP_VERSION, [
            '2026-10-30T15:00:01Z [backing_up] Pausing saving and making a recovery backup…',
            '2026-10-30T15:00:20Z Recovery backup statement-full-backup-x.tar.gz (21728081 bytes, 238 receipts), cloud backup 6f9a9787',
        ])
        self.assertEqual(m.this_instance_id(), '8935cefe7bcb')
        with m.app.app_context():
            self.assertTrue(m.cloud_backup_owned_here())

    def test_server_that_did_not_make_the_cloud_backup_does_not_take_over(self):
        m = self.module
        self.set_owner('8935cefe7bcb')
        # A copy of the live data on a test server: its update made only a local backup.
        self.finished_update('v' + m.APP_VERSION, [
            '2026-10-30T15:00:20Z Recovery backup statement-full-backup-x.tar.gz (21728081 bytes, 238 receipts)',
        ])
        self.assertNotEqual(m.this_instance_id(), '8935cefe7bcb')

    def test_log_from_another_version_is_not_used(self):
        m = self.module
        self.set_owner('8935cefe7bcb')
        self.finished_update('v1.0.0', ['x Recovery backup b.tar.gz (1 bytes, 0 receipts), cloud backup 6f9a9787'])
        self.assertNotEqual(m.this_instance_id(), '8935cefe7bcb')

    def test_restic_cache_is_kept_on_the_server(self):
        m = self.module
        config = cloud_backup.B2Config('us-east-005', 'acme-backups', 'p', 'KEYID12345', 'SECRETAPPKEY123', 'RECOVERY-KEY-VALUE')
        self.assertEqual(m.make_restic(config).cache_dir, m.BACKUP_DIR / '.restic-cache')


class StuckFileTests(unittest.TestCase):
    def fake_restic(self, code):
        return [sys.executable, '-c', code]

    def test_stops_quickly_when_one_file_keeps_failing(self):
        retrying = (
            "import sys, time\n"
            "sys.stderr.write('repository a1b2 opened\\n')\n"
            "for i in range(50):\n"
            "    sys.stderr.write('Load(<snapshot/4c58ada185>, 0, 0) returned error, retrying after 610ms: '"
            " 'incident id 4fe227e03198\\n'); sys.stderr.flush(); time.sleep(0.05)\n"
            "time.sleep(60)\n"
        )
        started = time.monotonic()
        with self.assertRaises(cloud_backup.FileKeepsFailing) as caught:
            cloud_backup.run_watched(self.fake_restic(retrying), dict(os.environ), None, timeout=60)
        self.assertLess(time.monotonic() - started, 20)
        self.assertIn('snapshot/4c58ada185', str(caught.exception))
        self.assertIn('status.backblaze.com', str(caught.exception))

    def test_a_few_retries_of_different_files_are_fine(self):
        code = (
            "import sys\n"
            "for name in ('data/aa', 'data/bb', 'index/cc'):\n"
            "    sys.stderr.write('Load(<%s>, 0, 0) returned error, retrying after 1s: too busy\\n' % name)\n"
            "print('[]')\n"
        )
        result = cloud_backup.run_watched(self.fake_restic(code), dict(os.environ), None, timeout=60)
        self.assertEqual((result.returncode, result.stdout.strip()), (0, '[]'))

    def test_timeout_still_applies(self):
        with self.assertRaises(cloud_backup.subprocess.TimeoutExpired):
            cloud_backup.run_watched(self.fake_restic('import time; time.sleep(30)'), dict(os.environ), None, timeout=1)

    def test_restic_run_reports_the_stuck_file_without_secrets(self):
        config = cloud_backup.B2Config('us-east-005', 'acme-backups', 'p', 'KEYID12345', 'SECRETAPPKEY123', 'RECOVERY-KEY-VALUE')
        restic = cloud_backup.Restic(config)
        restic._key_checked = True
        stuck = cloud_backup.FileKeepsFailing('Backblaze keeps refusing one backup file (snapshot/4c58): SECRETAPPKEY123')
        with patch('cloud_backup.restic_available', return_value=True), \
                patch('cloud_backup.run_watched', side_effect=stuck):
            with self.assertRaises(cloud_backup.CloudBackupError) as caught:
                restic.run(['snapshots'])
        self.assertIn('snapshot/4c58', str(caught.exception))
        self.assertNotIn('SECRETAPPKEY123', str(caught.exception))


if __name__ == '__main__':
    unittest.main()
