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
        self.assertIn('chmod -R u+rwX', entrypoint, 'root-era files with mode 000 must become readable')
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
            for name in ('init_db', 'ensure_seeded', 'resequence_all_clients',
                         'start_cloud_backup_scheduler', 'start_update_threads'):
                setattr(module, name, lambda name=name: calls.append(name))
            module.start_app_services()
            module.start_app_services()
        self.assertEqual(calls, ['init_db', 'ensure_seeded', 'resequence_all_clients',
                                 'start_cloud_backup_scheduler', 'start_update_threads'])

    @unittest.skipIf(os.name == 'nt', 'Windows has no Unix file modes')
    def test_restored_receipts_get_normal_permissions(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        with patch.dict(os.environ, {
            'DATABASE_PATH': str(base / 'test.db'), 'UPLOAD_DIR': str(base / 'uploads'),
            'BACKUP_DIR': str(base / 'backups'), 'SECRET_KEY': 'test-only',
        }):
            spec = importlib.util.spec_from_file_location('perm_app', ROOT / 'app.py')
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        staged = base / 'staged'
        staged.mkdir()
        receipt = staged / 'receipt.jpg'
        receipt.write_bytes(b'jpg')
        receipt.chmod(0)  # as found on the live server
        module.replace_uploads_contents(staged)
        self.assertEqual((base / 'uploads' / 'receipt.jpg').stat().st_mode & 0o777, 0o644)

    def test_installer_caps_memory(self):
        self.assertIn('mem_limit:', (ROOT / 'scripts' / 'install-updater.sh').read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()


class PdfFetchTests(unittest.TestCase):
    """PDFs may load the app's own logo, never web addresses or other server files."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        base = Path(cls.tmp.name)
        with patch.dict(os.environ, {
            'DATABASE_PATH': str(base / 'test.db'), 'UPLOAD_DIR': str(base / 'uploads'),
            'BACKUP_DIR': str(base / 'backups'), 'SECRET_KEY': 'test-only',
        }):
            spec = importlib.util.spec_from_file_location('pdf_app', ROOT / 'app.py')
            cls.module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(cls.module)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_only_the_apps_static_files_are_allowed(self):
        logo = (ROOT / 'static' / 'logo.png').resolve().as_uri()
        self.assertTrue(self.module.pdf_url_allowed(logo))
        for url in ['http://169.254.169.254/latest/meta-data', 'https://example.com/x.png',
                    (ROOT / 'app.py').resolve().as_uri(), (ROOT / 'static' / '..' / 'app.py').resolve().as_uri(),
                    'file:///etc/passwd', 'data:image/png;base64,AAAA', (ROOT / 'static' / 'missing.png').resolve().as_uri()]:
            self.assertFalse(self.module.pdf_url_allowed(url), url)

    def test_pinned_versions_have_no_known_problems_listed_here(self):
        pins = dict(line.split('==') for line in (ROOT / 'requirements.txt').read_text().split() if '==' in line)
        self.assertGreaterEqual(tuple(int(x) for x in pins['Flask'].split('.')), (3, 1, 3))
        self.assertGreaterEqual(tuple(int(x) for x in pins['requests'].split('.')), (2, 33, 0))
        self.assertGreaterEqual(int(pins['weasyprint'].split('.')[0]), 70)
        self.assertIn('openrouter', pins, 'openrouter must be pinned')
        constraints = (ROOT / 'constraints.txt').read_text()
        for name, version in pins.items():
            self.assertRegex(constraints, rf'(?im)^{name}=={re.escape(version)}$', name)
