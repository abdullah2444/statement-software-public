"""The production web server setup: one Gunicorn worker, non-root container, start-up runs once."""
import importlib.util
import os
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).parents[1]


class ProductionServerTests(unittest.TestCase):
    def test_gunicorn_runs_exactly_one_worker(self):
        spec = importlib.util.spec_from_file_location('gunicorn_conf', ROOT / 'gunicorn.conf.py')
        conf = importlib.util.module_from_spec(spec)
        with patch.dict(os.environ, {'HOST': '0.0.0.0', 'PORT': '18451'}):
            spec.loader.exec_module(conf)
        # More workers would each run their own backup and update jobs.
        self.assertEqual(conf.workers, 1)
        self.assertEqual(conf.worker_class, 'gthread')
        self.assertEqual(conf.bind, '0.0.0.0:18451')
        self.assertFalse(hasattr(conf, 'max_requests'), 'worker restarts would cut off running backups')

    def test_container_starts_gunicorn_as_app_user(self):
        dockerfile = (ROOT / 'Dockerfile').read_text(encoding='utf-8')
        self.assertIn('ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]', dockerfile)
        self.assertIn('"gunicorn", "-c", "gunicorn.conf.py", "wsgi:application"', dockerfile)
        self.assertIn('HEALTHCHECK', dockerfile)
        copied = re.search(r'^COPY VERSION (.+) \./$', dockerfile, re.M).group(1).split()
        for name in ('wsgi.py', 'gunicorn.conf.py', 'update_helper.py'):
            self.assertIn(name, copied)
        entrypoint = (ROOT / 'docker-entrypoint.sh').read_text(encoding='utf-8')
        self.assertIn('chown -R app:app', entrypoint)
        self.assertIn('setpriv --reuid=app --regid=app', entrypoint)
        self.assertNotIn('\r', entrypoint, 'a Windows line ending would break the script on Linux')
        self.assertRegex((ROOT / 'requirements.txt').read_text(), r'(?m)^gunicorn==')

    def test_start_up_runs_once_per_process(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        with patch.dict(os.environ, {
            'DATABASE_PATH': str(base / 'test.db'), 'UPLOAD_DIR': str(base / 'uploads'),
            'BACKUP_DIR': str(base / 'backups'), 'SECRET_KEY': 'test-only',
        }):
            spec = importlib.util.spec_from_file_location('prod_app', ROOT / 'app.py')
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            calls = []
            for name in ('init_db', 'ensure_seeded', 'resequence_all_clients', 'start_backup_scheduler',
                         'start_cloud_backup_scheduler', 'start_update_threads'):
                setattr(module, name, lambda name=name: calls.append(name))
            module.start_app_services()
            module.start_app_services()
        self.assertEqual(calls, ['init_db', 'ensure_seeded', 'resequence_all_clients', 'start_backup_scheduler',
                                 'start_cloud_backup_scheduler', 'start_update_threads'])

    def test_installer_caps_memory(self):
        self.assertIn('mem_limit:', (ROOT / 'scripts' / 'install-updater.sh').read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
