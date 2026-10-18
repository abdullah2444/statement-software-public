import importlib.util
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT))

import cloud_backup  # noqa: E402

HAVE_RESTIC = shutil.which("restic") is not None
CONFIG = {
    "cloud_backup_region": "us-west-004",
    "cloud_backup_bucket": "test-bucket-123",
    "cloud_backup_prefix": "statement-software",
    "cloud_backup_key_id": "0040000000000000000000001",
    "cloud_backup_app_key": "K004FAKEAPPLICATIONKEYVALUE12345",
    "cloud_backup_recovery_key": "AAAA-BBBB-CCCC-DDDD-EEEE-FFFF-0000-1111",
    "cloud_backup_recovery_key_confirmed": "1",
}


class CloudBackupTests(unittest.TestCase):
    """Uses a real restic repository in a local folder in place of the B2 bucket."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        base = Path(cls.tmp.name)
        cls.repo_dir = base / "restic-repo"
        (base / "data").mkdir()
        # Kept for the whole class: restoring re-runs init_db, which reads SECRET_KEY.
        cls.env = patch.dict(os.environ, {
            "DATABASE_PATH": str(base / "data" / "test.db"),
            "UPLOAD_DIR": str(base / "data" / "uploads"),
            "BACKUP_DIR": str(base / "data" / "backups"),
            "SECRET_KEY": "test-only", "INITIAL_ADMIN_USERNAME": "admin",
            "INITIAL_ADMIN_PASSWORD": "admin-pass-1",
            "CLOUD_BACKUP_REPOSITORY_OVERRIDE": str(cls.repo_dir),
        })
        cls.env.start()
        spec = importlib.util.spec_from_file_location("cloud_app", ROOT / "app.py")
        cls.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.module)
        cls.module.init_db()
        cls.app = cls.module.app
        cls.app.config["TESTING"] = True
        with cls.app.app_context():
            db = cls.module.get_db()
            db.execute(
                "insert into users(username, password_hash, role, must_change_password, created_at) values (?, ?, 'user', 0, ?)",
                ("clerk", cls.module.generate_password_hash("clerk-pass-1"), cls.module.utc_timestamp()),
            )
            db.commit()

    @classmethod
    def tearDownClass(cls):
        cls.env.stop()
        cls.tmp.cleanup()

    def setUp(self):
        shutil.rmtree(self.repo_dir, ignore_errors=True)
        with self.app.app_context():
            db = self.module.get_db()
            db.execute("delete from app_settings where key like 'cloud_backup_%'")
            db.commit()
        uploads = self.module.UPLOAD_DIR
        shutil.rmtree(uploads, ignore_errors=True)
        uploads.mkdir(parents=True)
        (uploads / "receipt-1.png").write_bytes(b"receipt one")

    def configure(self, **overrides):
        with self.app.app_context():
            for key, value in {**CONFIG, **overrides}.items():
                self.module.set_setting(key, value)
            restic = self.module.make_restic(self.module.load_cloud_backup_config())
            if not restic.exists():
                restic.init()
            return restic

    def login(self, client, username="admin", password="admin-pass-1"):
        with client.session_transaction() as sess:
            sess["_csrf_token"] = "test-csrf"
        user_id = self.user_id(username)
        with client.session_transaction() as sess:
            sess["user_id"] = user_id
        return "test-csrf"

    def user_id(self, username):
        with self.app.app_context():
            return self.module.get_db().execute("select id from users where username = ?", (username,)).fetchone()["id"]

    def set_client_name(self, name):
        with self.app.app_context():
            db = self.module.get_db()
            db.execute("delete from clients")
            db.execute("insert into clients(name) values (?)", (name,))
            db.commit()

    def client_names(self):
        with sqlite3.connect(self.module.DB_PATH) as db:
            return [row[0] for row in db.execute("select name from clients")]

    # ── Config validation ────────────────────────────────────────────────

    def test_endpoint_must_be_backblaze(self):
        self.assertEqual(cloud_backup.parse_region("s3.us-west-004.backblazeb2.com"), "us-west-004")
        self.assertEqual(cloud_backup.parse_region("https://s3.eu-central-003.backblazeb2.com/"), "eu-central-003")
        for bad in ["http://169.254.169.254", "s3.us-west-004.backblazeb2.com.evil.net", "localhost:9000",
                    "s3.amazonaws.com", "us-west-004/../x"]:
            with self.assertRaises(cloud_backup.CloudBackupError, msg=bad):
                cloud_backup.parse_region(bad)

    def test_prefix_cannot_escape_bucket(self):
        with self.assertRaises(cloud_backup.CloudBackupError):
            cloud_backup.validate_config("us-west-004", "test-bucket-123", "../other", "0040000000000000000000001",
                                         "K004FAKEAPPLICATIONKEYVALUE12345", "x" * 20)

    def test_errors_never_contain_secrets(self):
        config = cloud_backup.B2Config("us-west-004", "b" * 8, "p", "KEYID12345", "SECRETAPPKEY123", "RECOVERY-KEY-VALUE")
        restic = cloud_backup.Restic(config)
        self.assertEqual(restic._scrub("bad key SECRETAPPKEY123 / RECOVERY-KEY-VALUE"), "bad key [hidden] / [hidden]")

    def b2_answer(self, buckets, capabilities=("listFiles", "readFiles", "writeFiles", "deleteFiles"), status=200):
        response = unittest.mock.Mock(status_code=status)
        response.json.return_value = {"apiInfo": {"storageApi": {
            "s3ApiUrl": "https://s3.us-east-005.backblazeb2.com",
            "allowed": {"buckets": [{"id": "b1", "name": name} for name in buckets], "capabilities": list(capabilities)},
        }}}
        return response

    def test_discover_finds_bucket_and_region_for_single_bucket_key(self):
        with patch("cloud_backup.requests.get", return_value=self.b2_answer(["acme-backups"])) as get:
            self.assertEqual(cloud_backup.discover_b2(CONFIG["cloud_backup_key_id"], CONFIG["cloud_backup_app_key"]),
                             ("us-east-005", "acme-backups"))
        self.assertEqual(get.call_args.kwargs["auth"], (CONFIG["cloud_backup_key_id"], CONFIG["cloud_backup_app_key"]))
        with patch("cloud_backup.requests.get", return_value=self.b2_answer([])):
            self.assertEqual(cloud_backup.discover_b2(CONFIG["cloud_backup_key_id"], CONFIG["cloud_backup_app_key"])[1], None)

    def test_discover_rejects_bad_or_read_only_keys(self):
        with patch("cloud_backup.requests.get", return_value=self.b2_answer([], status=401)):
            with self.assertRaisesRegex(cloud_backup.CloudBackupError, "didn't accept"):
                cloud_backup.discover_b2(CONFIG["cloud_backup_key_id"], CONFIG["cloud_backup_app_key"])
        with patch("cloud_backup.requests.get", return_value=self.b2_answer(["acme-backups"], capabilities=("listFiles", "readFiles"))):
            with self.assertRaisesRegex(cloud_backup.CloudBackupError, "Read and Write"):
                cloud_backup.discover_b2(CONFIG["cloud_backup_key_id"], CONFIG["cloud_backup_app_key"])

    @unittest.skipUnless(HAVE_RESTIC, "restic not installed")
    def test_setup_with_only_the_two_keys(self):
        client = self.app.test_client()
        csrf = self.login(client)
        form = {"csrf_token": csrf, "cloud_backup_key_id": CONFIG["cloud_backup_key_id"],
                "cloud_backup_app_key": CONFIG["cloud_backup_app_key"],
                "cloud_backup_interval_hours": "0.5", "cloud_backup_retention_days": "7"}
        with patch("cloud_backup.requests.get", return_value=self.b2_answer([])):
            client.post("/settings/cloud-backup/save", data=form)
        with self.app.app_context():
            self.assertFalse(self.module.cloud_backup_configured(), "a key for every bucket needs a bucket name")
        with patch("cloud_backup.requests.get", return_value=self.b2_answer(["acme-backups"])):
            client.post("/settings/cloud-backup/save", data=form)
        with self.app.app_context():
            self.assertTrue(self.module.cloud_backup_configured())
            self.assertEqual(self.module.get_setting("cloud_backup_bucket"), "acme-backups")
            self.assertEqual(self.module.get_setting("cloud_backup_region"), "us-east-005")
            self.assertEqual(self.module.get_setting("cloud_backup_prefix"), "statement-software")

    # ── Consistency gate ─────────────────────────────────────────────────

    def test_restore_waits_for_in_flight_change_and_holds_new_ones(self):
        gate = self.module.DataGate()
        self.assertTrue(gate.acquire_shared(timeout=1))
        got, finish = [], threading.Event()

        def restore():
            got.append(gate.acquire_exclusive(timeout=5))
            finish.wait(5)
            gate.release_exclusive()

        worker = threading.Thread(target=restore)
        worker.start()
        time.sleep(0.2)
        self.assertEqual(got, [], "restore must wait while a change is being saved")
        gate.release_shared()
        time.sleep(0.3)
        self.assertEqual(got, [True])
        self.assertFalse(gate.acquire_shared(timeout=0.2), "new changes wait while the restore runs")
        finish.set()
        worker.join(2)
        self.assertTrue(gate.acquire_shared(timeout=1))
        gate.release_shared()

    def test_request_holding_gate_can_take_exclusive(self):
        gate = self.module.DataGate()
        self.assertTrue(gate.acquire_shared(timeout=1))
        self.assertTrue(gate.acquire_exclusive(timeout=1))
        gate.release_exclusive()
        gate.release_shared()

    def test_hold_left_by_finished_thread_does_not_block(self):
        gate = self.module.DataGate()
        leaker = threading.Thread(target=lambda: gate.acquire_shared(timeout=1))
        leaker.start()
        leaker.join()
        self.assertTrue(gate.acquire_exclusive(timeout=1))
        gate.release_exclusive()

    def test_snapshot_keeps_receipt_deleted_afterwards(self):
        staging = Path(self.tmp.name) / "staging"
        with self.app.app_context():
            self.module.stage_consistent_snapshot(staging)
        (self.module.UPLOAD_DIR / "receipt-1.png").unlink()
        self.assertEqual((staging / "uploads" / "receipt-1.png").read_bytes(), b"receipt one")
        manifest = json.loads((staging / "manifest.json").read_text())
        self.assertEqual(manifest["upload_file_count"], 1)
        shutil.rmtree(staging)

    def test_backup_not_blocked_by_slow_request(self):
        """A request that is still saving must never make a backup wait or fail."""
        staging = Path(self.tmp.name) / "staging-busy"
        self.assertTrue(self.module.DATA_GATE.acquire_shared(timeout=1))
        try:
            done = []
            worker = threading.Thread(target=lambda: (self.app.app_context().push(),
                                                      self.module.stage_consistent_snapshot(staging),
                                                      done.append(True)))
            worker.start()
            worker.join(10)
            self.assertEqual(done, [True])
        finally:
            self.module.DATA_GATE.release_shared()
        shutil.rmtree(staging)

    def test_snapshot_includes_receipt_saved_during_backup(self):
        staging = Path(self.tmp.name) / "staging-new"
        real_snapshot = self.module.snapshot_database

        def snapshot_then_new_receipt(target):
            # Simulates a receipt saved while the database is being copied.
            result = real_snapshot(target)
            (self.module.UPLOAD_DIR / "receipt-2.png").write_bytes(b"receipt two")
            return result

        with self.app.app_context(), patch.object(self.module, "snapshot_database", snapshot_then_new_receipt):
            self.module.stage_consistent_snapshot(staging)
        self.assertEqual((staging / "uploads" / "receipt-2.png").read_bytes(), b"receipt two")
        shutil.rmtree(staging)

    # ── Backup, verify, restore with real restic ─────────────────────────

    @unittest.skipUnless(HAVE_RESTIC, "restic not installed")
    def test_backup_verify_and_restore_round_trip(self):
        self.configure()
        self.set_client_name("Before Backup")
        with self.app.app_context():
            result = self.module.run_cloud_backup()
            self.assertTrue(result["ok"], result)
            self.assertTrue(self.module.get_setting("cloud_backup_last_success"))
            self.assertEqual(self.module.get_setting("cloud_backup_last_error"), "")
            verify = self.module.run_cloud_verify()
            self.assertTrue(verify["ok"], verify)
            snapshot_id = self.module.make_restic(self.module.load_cloud_backup_config()).snapshots()[0]["id"]
        # Change data, then restore the backup through the UI route.
        self.set_client_name("After Backup")
        (self.module.UPLOAD_DIR / "receipt-1.png").unlink()
        client = self.app.test_client()
        csrf = self.login(client)
        response = client.post("/settings/cloud-backup/restore",
                               data={"csrf_token": csrf, "snapshot_id": snapshot_id, "confirm_text": "RESTORE"})
        self.assertEqual(response.status_code, 302)
        with client.session_transaction() as sess:
            flashes = sess.get("_flashes")
        self.assertIn("/login", response.headers["Location"], flashes)
        self.assertEqual(self.client_names(), ["Before Backup"])
        self.assertEqual((self.module.UPLOAD_DIR / "receipt-1.png").read_bytes(), b"receipt one")
        safety = [p.name for p in self.module.BACKUP_DIR.iterdir() if "before-cloud" in p.name]
        self.assertTrue(safety, "a safety backup of the current data is made first")

    @unittest.skipUnless(HAVE_RESTIC, "restic not installed")
    def test_restore_needs_typed_confirmation(self):
        self.configure()
        self.set_client_name("Live Data")
        with self.app.app_context():
            self.assertTrue(self.module.run_cloud_backup()["ok"])
            snapshot_id = self.module.make_restic(self.module.load_cloud_backup_config()).snapshots()[0]["id"]
        self.set_client_name("Newer Live Data")
        client = self.app.test_client()
        csrf = self.login(client)
        client.post("/settings/cloud-backup/restore", data={"csrf_token": csrf, "snapshot_id": snapshot_id, "confirm_text": "yes"})
        self.assertEqual(self.client_names(), ["Newer Live Data"])

    @unittest.skipUnless(HAVE_RESTIC, "restic not installed")
    def test_wrong_recovery_key_is_rejected(self):
        self.configure()
        with self.app.app_context():
            self.module.set_setting("cloud_backup_recovery_key", "WRONG-WRONG-WRONG-WRONG")
            result = self.module.run_cloud_backup()
        self.assertFalse(result["ok"])
        self.assertIn("recovery key does not match", result["error"])
        self.assertNotIn("WRONG-WRONG", result["error"])

    @unittest.skipUnless(HAVE_RESTIC, "restic not installed")
    def test_tampered_backup_fails_check_and_restore_keeps_live_data(self):
        self.configure()
        self.set_client_name("Good Data")
        with self.app.app_context():
            self.assertTrue(self.module.run_cloud_backup()["ok"])
            snapshot_id = self.module.make_restic(self.module.load_cloud_backup_config()).snapshots()[0]["id"]
        for pack in (self.repo_dir / "data").rglob("*"):
            if pack.is_file():
                pack.chmod(0o644)
                raw = bytearray(pack.read_bytes())
                raw[len(raw) // 2] ^= 0xFF
                pack.write_bytes(bytes(raw))
        with self.app.app_context():
            self.module.set_setting("cloud_backup_last_error", "")
            # restic caches data locally; clear it so the damaged copy is read.
            shutil.rmtree(self.module.DATA_DIR / ".restic-cache", ignore_errors=True)
            self.assertFalse(self.module.run_cloud_verify()["ok"])
        self.set_client_name("Current Data")
        client = self.app.test_client()
        csrf = self.login(client)
        client.post("/settings/cloud-backup/restore", data={"csrf_token": csrf, "snapshot_id": snapshot_id, "confirm_text": "RESTORE"})
        self.assertEqual(self.client_names(), ["Current Data"], "a failed restore leaves current data alone")

    @unittest.skipUnless(HAVE_RESTIC, "restic not installed")
    def test_download_gives_normal_full_backup_file(self):
        self.configure()
        with self.app.app_context():
            self.assertTrue(self.module.run_cloud_backup()["ok"])
            snapshot_id = self.module.make_restic(self.module.load_cloud_backup_config()).snapshots()[0]["id"]
        client = self.app.test_client()
        self.login(client)
        response = client.get(f"/settings/cloud-backup/download/{snapshot_id}")
        self.assertEqual(response.status_code, 200)
        archive = Path(self.tmp.name) / "downloaded.tar.gz"
        archive.write_bytes(response.data)
        response.close()
        with self.app.app_context():
            candidate, uploads, _name, kind, cleanup = self.module.stage_restore_source(archive, archive.name)
            self.assertEqual(kind, "full")
            self.assertTrue((uploads / "receipt-1.png").exists())
            shutil.rmtree(cleanup)

    # ── Retention ────────────────────────────────────────────────────────

    def test_retention_never_runs_after_failed_backup(self):
        with self.app.app_context():
            for key, value in CONFIG.items():
                self.module.set_setting(key, value)
            with patch.object(cloud_backup.Restic, "unlock"), \
                 patch.object(cloud_backup.Restic, "backup", side_effect=cloud_backup.CloudBackupError("upload cut off")), \
                 patch.object(cloud_backup.Restic, "forget") as forget:
                result = self.module.run_cloud_backup()
        self.assertFalse(result["ok"])
        forget.assert_not_called()

    def test_retention_runs_after_verified_backup_and_prunes_daily(self):
        with self.app.app_context():
            for key, value in CONFIG.items():
                self.module.set_setting(key, value)
            summary = {"snapshot_id": "abcdef0123456789", "data_added": 10}
            with patch.object(cloud_backup.Restic, "unlock"), \
                 patch.object(cloud_backup.Restic, "backup", return_value=summary), \
                 patch.object(cloud_backup.Restic, "snapshots", return_value=[{"id": "abcdef0123456789"}]), \
                 patch.object(cloud_backup.Restic, "stats_bytes", return_value=2048), \
                 patch.object(cloud_backup.Restic, "forget") as forget:
                self.assertTrue(self.module.run_cloud_backup()["ok"])
                self.assertTrue(self.module.run_cloud_backup()["ok"])
            self.assertEqual(forget.call_args_list[0].kwargs, {"prune": True})
            self.assertEqual(forget.call_args_list[1].kwargs, {"prune": False})
            self.assertEqual(forget.call_args_list[0].args, (7,))
            self.assertEqual(self.module.get_setting("cloud_backup_storage_bytes"), "2048")

    def test_backup_not_counted_if_missing_after_upload(self):
        with self.app.app_context():
            for key, value in CONFIG.items():
                self.module.set_setting(key, value)
            with patch.object(cloud_backup.Restic, "unlock"), \
                 patch.object(cloud_backup.Restic, "backup", return_value={"snapshot_id": "abcdef0123456789"}), \
                 patch.object(cloud_backup.Restic, "snapshots", return_value=[]), \
                 patch.object(cloud_backup.Restic, "forget") as forget:
                result = self.module.run_cloud_backup()
            self.assertFalse(result["ok"])
            self.assertEqual(self.module.get_setting("cloud_backup_last_success"), "")
            forget.assert_not_called()

    # ── Scheduling and warnings ──────────────────────────────────────────

    def test_schedule_and_retry(self):
        with self.app.app_context():
            for key, value in CONFIG.items():
                self.module.set_setting(key, value)
            self.module.set_setting("cloud_backup_interval_hours", "0.5")
            now = self.module.utc_now()
            self.assertTrue(self.module.cloud_backup_due(now))
            stamp = (now - timedelta(minutes=12)).isoformat()
            self.module.set_setting("cloud_backup_last_attempt", stamp)
            self.module.set_setting("cloud_backup_last_success", stamp)
            self.assertFalse(self.module.cloud_backup_due(now), "succeeded 12 minutes ago, next in 30")
            self.module.set_setting("cloud_backup_last_success", (now - timedelta(hours=5)).isoformat())
            self.assertTrue(self.module.cloud_backup_due(now), "failed 12 minutes ago, retry after 10")

    def test_stale_backup_alert(self):
        with self.app.app_context():
            for key, value in CONFIG.items():
                self.module.set_setting(key, value)
            self.module.set_setting("cloud_backup_last_success", self.module.utc_timestamp())
            self.assertEqual(self.module.cloud_backup_alert(), "")
            self.module.set_setting("cloud_backup_last_success", (self.module.utc_now() - timedelta(hours=7)).isoformat())
            self.assertIn("have not worked for 7 hours", self.module.cloud_backup_alert())
            self.module.set_setting("cloud_backup_recovery_key_confirmed", "")
            self.assertIn("recovery key", self.module.cloud_backup_alert())

    def test_only_one_scheduler_per_installation(self):
        if self.module.fcntl is None:
            self.skipTest("no fcntl on this platform")
        lock_path = self.module.DATA_DIR / ".cloud-backup-scheduler.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with open(lock_path, "w") as other_process:
            self.module.fcntl.flock(other_process, self.module.fcntl.LOCK_EX | self.module.fcntl.LOCK_NB)
            self.assertFalse(self.module.start_cloud_backup_scheduler())

    # ── Settings page, roles, CSRF, secrets ──────────────────────────────

    @unittest.skipUnless(HAVE_RESTIC, "restic not installed")
    def test_setup_generates_key_shows_it_once_and_hides_secrets(self):
        client = self.app.test_client()
        csrf = self.login(client)
        form = {
            "csrf_token": csrf,
            "cloud_backup_endpoint": "s3.us-west-004.backblazeb2.com",
            "cloud_backup_bucket": CONFIG["cloud_backup_bucket"],
            "cloud_backup_prefix": "statement-software",
            "cloud_backup_key_id": CONFIG["cloud_backup_key_id"],
            "cloud_backup_app_key": CONFIG["cloud_backup_app_key"],
            "cloud_backup_interval_hours": "0.5",
            "cloud_backup_retention_days": "7",
        }
        self.assertEqual(client.post("/settings/cloud-backup/save", data=form).status_code, 302)
        with self.app.app_context():
            key = self.module.get_setting("cloud_backup_recovery_key")
            audit = json.dumps([dict(r) for r in self.module.get_db().execute("select * from api_audit_log").fetchall()]) \
                if self.module.get_db().execute("select name from sqlite_master where name='api_audit_log'").fetchone() else ""
        self.assertRegex(key, r"^([0-9A-F]{4}-){7}[0-9A-F]{4}$")
        page = client.get("/settings").get_data(as_text=True)
        self.assertIn(key, page, "new key is shown until confirmed")
        self.assertNotIn(CONFIG["cloud_backup_app_key"], page)
        self.assertNotIn(CONFIG["cloud_backup_app_key"], audit)
        status = client.get("/api/cloud-backup/status").get_data(as_text=True)
        self.assertNotIn(key, status)
        self.assertNotIn(CONFIG["cloud_backup_app_key"], status)

        client.post("/settings/cloud-backup/confirm-key", data={"csrf_token": csrf, "confirm_last4": "zzzz"})
        self.assertIn(key, client.get("/settings").get_data(as_text=True))
        client.post("/settings/cloud-backup/confirm-key", data={"csrf_token": csrf, "confirm_last4": key[-4:].lower()})
        self.assertNotIn(key, client.get("/settings").get_data(as_text=True))
        wrong = client.post("/settings/cloud-backup/reveal-key", data={"csrf_token": csrf, "current_password": "nope"})
        self.assertNotIn(key, wrong.get_data(as_text=True))
        right = client.post("/settings/cloud-backup/reveal-key", data={"csrf_token": csrf, "current_password": "admin-pass-1"})
        self.assertIn(key, right.get_data(as_text=True))

        # A second installation pointing at the same folder must bring the existing key.
        with self.app.app_context():
            self.module.get_db().execute("delete from app_settings where key like 'cloud_backup_%'")
            self.module.get_db().commit()
        client.post("/settings/cloud-backup/save", data=form)
        with self.app.app_context():
            self.assertEqual(self.module.get_setting("cloud_backup_recovery_key"), "")
        client.post("/settings/cloud-backup/save", data={**form, "cloud_backup_recovery_key": key})
        with self.app.app_context():
            self.assertEqual(self.module.get_setting("cloud_backup_recovery_key"), key)
            self.assertEqual(self.module.get_setting("cloud_backup_recovery_key_confirmed"), "1")

    def test_ordinary_users_cannot_use_cloud_backup(self):
        client = self.app.test_client()
        csrf = self.login(client, "clerk")
        for method, url in [("post", "/api/cloud-backup/run"), ("post", "/api/cloud-backup/verify"),
                            ("get", "/api/cloud-backup/status"), ("get", "/api/cloud-backup/snapshots"),
                            ("post", "/settings/cloud-backup/save"), ("post", "/settings/cloud-backup/restore"),
                            ("post", "/settings/cloud-backup/disconnect"), ("post", "/settings/cloud-backup/reveal-key"),
                            ("get", "/settings/cloud-backup/download/abcdef01")]:
            response = getattr(client, method)(url, data={"csrf_token": csrf} if method == "post" else None)
            self.assertEqual(response.status_code, 403, url)

    def test_cloud_actions_require_csrf(self):
        client = self.app.test_client()
        self.login(client)
        for url in ["/api/cloud-backup/run", "/settings/cloud-backup/save", "/settings/cloud-backup/restore",
                    "/settings/cloud-backup/disconnect"]:
            self.assertEqual(client.post(url, headers={"X-Requested-With": "XMLHttpRequest"}).status_code, 400, url)

    def test_disconnect_keeps_cloud_data_and_forgets_keys(self):
        with self.app.app_context():
            for key, value in CONFIG.items():
                self.module.set_setting(key, value)
        client = self.app.test_client()
        csrf = self.login(client)
        client.post("/settings/cloud-backup/disconnect", data={"csrf_token": csrf})
        with self.app.app_context():
            self.assertFalse(self.module.cloud_backup_configured())
            self.assertEqual(self.module.get_setting("cloud_backup_app_key"), "")


if __name__ == "__main__":
    unittest.main()
