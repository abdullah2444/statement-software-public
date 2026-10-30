"""A second copy of every cloud backup in Cloudflare R2: connecting it, backing up to both,
one copy failing without stopping the other, and listing or restoring from either."""
from datetime import datetime, timedelta, timezone
import importlib.util
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import unittest
import unittest.mock
from unittest.mock import patch

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT))

import cloud_backup  # noqa: E402

HAVE_RESTIC = shutil.which("restic") is not None
ACCOUNT = "0123456789abcdef0123456789abcdef"
R2_KEY_ID = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6"
R2_SECRET = "0f1e2d3c4b5a69788796a5b4c3d2e1f00f1e2d3c4b5a69788796a5b4c3d2e1f0"
B2_SETTINGS = {
    "cloud_backup_region": "us-west-004",
    "cloud_backup_bucket": "test-bucket-123",
    "cloud_backup_prefix": "statement-software",
    "cloud_backup_key_id": "0040000000000000000000001",
    "cloud_backup_app_key": "K004FAKEAPPLICATIONKEYVALUE12345",
    "cloud_backup_recovery_key": "AAAA-BBBB-CCCC-DDDD-EEEE-FFFF-0000-1111",
    "cloud_backup_recovery_key_confirmed": "1",
}
R2_SETTINGS = {
    "cloud_backup_r2_host": f"{ACCOUNT}.r2.cloudflarestorage.com",
    "cloud_backup_r2_bucket": "statement-backups",
    "cloud_backup_r2_prefix": "statement-software",
    "cloud_backup_r2_key_id": R2_KEY_ID,
    "cloud_backup_r2_secret_key": R2_SECRET,
}


def r2_config(**changes):
    values = dict(host=f"{ACCOUNT}.r2.cloudflarestorage.com", bucket="statement-backups", prefix="statement-software",
                  key_id=R2_KEY_ID, app_key=R2_SECRET, recovery_key="AAAA-BBBB-CCCC-DDDD")
    values.update(changes)
    return cloud_backup.R2Config(**values)


class R2ModuleTests(unittest.TestCase):
    def test_address_from_the_cloudflare_bucket_page(self):
        self.assertEqual(cloud_backup.parse_r2_endpoint(f"https://{ACCOUNT}.r2.cloudflarestorage.com/statement-backups"),
                         (f"{ACCOUNT}.r2.cloudflarestorage.com", "statement-backups"))
        self.assertEqual(cloud_backup.parse_r2_endpoint(f"{ACCOUNT}.eu.r2.cloudflarestorage.com/"),
                         (f"{ACCOUNT}.eu.r2.cloudflarestorage.com", ""))
        for wrong in ("", "https://s3.us-east-005.backblazeb2.com/bucket", "https://evil.example.com/x",
                      f"https://{ACCOUNT}.r2.cloudflarestorage.com.evil.com/b"):
            with self.assertRaisesRegex(cloud_backup.CloudBackupError, "S3 API address"):
                cloud_backup.parse_r2_endpoint(wrong)

    def test_settings_are_checked(self):
        config = cloud_backup.validate_r2_config(f"https://{ACCOUNT}.r2.cloudflarestorage.com/statement-backups", "",
                                                 "statement-software", R2_KEY_ID, R2_SECRET, "AAAA-BBBB-CCCC-DDDD")
        self.assertEqual(config.repository, f"s3:https://{ACCOUNT}.r2.cloudflarestorage.com/statement-backups/statement-software")
        address = f"https://{ACCOUNT}.r2.cloudflarestorage.com"
        cases = [
            ((address, "", "p", R2_KEY_ID, R2_SECRET, "AAAA-BBBB-CCCC-DDDD"), "Bucket name"),
            ((address, "Bad_Bucket", "p", R2_KEY_ID, R2_SECRET, "AAAA-BBBB-CCCC-DDDD"), "Bucket name"),
            ((address, "bucket", "../x", R2_KEY_ID, R2_SECRET, "AAAA-BBBB-CCCC-DDDD"), "Folder name"),
            ((address, "bucket", "p", "short", R2_SECRET, "AAAA-BBBB-CCCC-DDDD"), "Access Key ID"),
            ((address, "bucket", "p", R2_KEY_ID, "", "AAAA-BBBB-CCCC-DDDD"), "Secret Access Key"),
            ((address, "bucket", "p", R2_KEY_ID, R2_SECRET, ""), "recovery key"),
        ]
        for args, message in cases:
            with self.assertRaisesRegex(cloud_backup.CloudBackupError, message):
                cloud_backup.validate_r2_config(*args)

    def test_request_signing_matches_amazons_library(self):
        # Expected value computed with botocore's SigV4Auth for the same request.
        headers = cloud_backup.sigv4_headers(
            f"{ACCOUNT}.r2.cloudflarestorage.com", "/my-bucket",
            {"list-type": "2", "max-keys": "1", "prefix": "statement-software/"},
            R2_KEY_ID, R2_SECRET, now=datetime(2026, 10, 30, 15, 0, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(headers["x-amz-date"], "20261030T150000Z")
        self.assertTrue(headers["Authorization"].startswith(
            f"AWS4-HMAC-SHA256 Credential={R2_KEY_ID}/20261030/auto/s3/aws4_request, "
            "SignedHeaders=host;x-amz-content-sha256;x-amz-date, Signature="))
        self.assertTrue(headers["Authorization"].endswith(
            "9b4a698f653e8f05962ae943cc69b11fcbe645627ca0d5402fe14a70a4c2cc89"))

    def answer(self, status, text=""):
        return unittest.mock.Mock(status_code=status, text=text)

    def test_key_check_explains_each_problem(self):
        cases = [
            (self.answer(403, "<Code>AccessDenied</Code>"), "refused the key"),
            (self.answer(404, "<Code>NoSuchBucket</Code>"), "no bucket named statement-backups"),
            (self.answer(500), r"error \(500\)"),
        ]
        for response, message in cases:
            with patch("cloud_backup.requests.get", return_value=response):
                with self.assertRaisesRegex(cloud_backup.CloudBackupError, message):
                    cloud_backup.Restic(r2_config()).check_key()
        with patch("cloud_backup.requests.get", side_effect=cloud_backup.requests.ConnectionError()):
            with self.assertRaisesRegex(cloud_backup.CloudBackupError, "Could not reach Cloudflare R2"):
                cloud_backup.Restic(r2_config()).check_key()
        with patch("cloud_backup.requests.get", return_value=self.answer(200, "<ListBucketResult/>")) as get:
            restic = cloud_backup.Restic(r2_config())
            restic.check_key()
            restic.check_key()
        self.assertEqual(get.call_count, 1, "checked once")
        self.assertEqual(get.call_args.kwargs["params"]["prefix"], "statement-software/")
        self.assertNotIn(R2_SECRET, str(get.call_args))

    def test_restic_is_pointed_at_r2(self):
        env = cloud_backup.Restic(r2_config())._env()
        self.assertEqual(env["RESTIC_REPOSITORY"], f"s3:https://{ACCOUNT}.r2.cloudflarestorage.com/statement-backups/statement-software")
        self.assertEqual((env["AWS_ACCESS_KEY_ID"], env["AWS_SECRET_ACCESS_KEY"], env["AWS_DEFAULT_REGION"]),
                         (R2_KEY_ID, R2_SECRET, "auto"))
        b2 = cloud_backup.B2Config("us-west-004", "bucket-123", "p", "KEYID12345", "SECRETAPPKEY123", "RECOVERY-KEY-VALUE")
        self.assertNotIn("AWS_DEFAULT_REGION", cloud_backup.Restic(b2)._env())

    def test_messages_name_cloudflare(self):
        restic = cloud_backup.Restic(r2_config())
        restic._key_checked = True
        refused = cloud_backup.subprocess.CompletedProcess([], 1, "", "Fatal: Stat: 403 Access Denied\n")
        with patch("cloud_backup.restic_available", return_value=True), \
                patch("cloud_backup.run_watched", return_value=refused) as run:
            with self.assertRaisesRegex(cloud_backup.CloudBackupError, "^Cloudflare R2 refused the key"):
                restic.run(["snapshots"])
        self.assertEqual(run.call_args.args[4:], ("Cloudflare R2", "www.cloudflarestatus.com"))


class FakeRestic:
    """Stands in for restic in one bucket: records backups, or fails."""

    def __init__(self, module, fail=""):
        self.module, self.fail, self.saved = module, fail, []

    def snapshots(self):
        return [{"id": snap, "time": "2026-10-30T15:00:00Z", "tags": [f"instance-{self.module.this_instance_id()}"]}
                for snap in self.saved]

    def unlock(self):
        pass

    def backup(self, source, tags=None):
        if self.fail:
            raise cloud_backup.CloudBackupError(self.fail)
        snap = f"{len(self.saved) + 1:02d}" + "ab" * 31
        self.saved.append(snap)
        return {"snapshot_id": snap[:8], "data_added": 100}

    def forget(self, days, prune=False):
        pass

    def stats_bytes(self):
        return 4096


class R2AppTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        base = Path(cls.tmp.name)
        cls.b2_repo, cls.r2_repo = base / "b2-repo", base / "r2-repo"
        (base / "data").mkdir()
        cls.env = patch.dict(os.environ, {
            "DATABASE_PATH": str(base / "data" / "test.db"), "UPLOAD_DIR": str(base / "data" / "uploads"),
            "BACKUP_DIR": str(base / "data" / "backups"), "SECRET_KEY": "test-only",
            "INITIAL_ADMIN_USERNAME": "admin", "INITIAL_ADMIN_PASSWORD": "admin-pass-1",
            "CLOUD_BACKUP_REPOSITORY_OVERRIDE": str(cls.b2_repo),
            "CLOUD_BACKUP_R2_REPOSITORY_OVERRIDE": str(cls.r2_repo),
        })
        cls.env.start()
        spec = importlib.util.spec_from_file_location("r2_app", ROOT / "app.py")
        cls.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.module)
        cls.module.init_db()
        cls.app = cls.module.app
        cls.app.config["TESTING"] = True

    @classmethod
    def tearDownClass(cls):
        cls.env.stop()
        cls.tmp.cleanup()

    def setUp(self):
        for repo in (self.b2_repo, self.r2_repo):
            shutil.rmtree(repo, ignore_errors=True)
        with self.app.app_context():
            db = self.module.get_db()
            db.execute("delete from app_settings where key like 'cloud_backup_%'")
            db.execute("delete from api_audit_log")
            db.commit()
        uploads = self.module.UPLOAD_DIR
        shutil.rmtree(uploads, ignore_errors=True)
        uploads.mkdir(parents=True)
        (uploads / "receipt-1.png").write_bytes(b"receipt one")

    def configure(self, r2=True):
        with self.app.app_context():
            for key, value in {**B2_SETTINGS, **(R2_SETTINGS if r2 else {})}.items():
                self.module.set_setting(key, value)
            self.module.set_setting("cloud_backup_instance", self.module.this_instance_id())

    def setting(self, key):
        with self.app.app_context():
            return self.module.get_setting(key, "")

    def admin_client(self):
        client = self.app.test_client()
        with self.app.app_context():
            user_id = self.module.get_db().execute("select id from users where username = 'admin'").fetchone()["id"]
        with client.session_transaction() as sess:
            sess["_csrf_token"], sess["user_id"] = "test-csrf", user_id
        return client

    def with_fakes(self, b2, r2):
        fakes = {"backblaze": b2, "r2": r2}
        return patch.object(self.module, "make_copy_restic", side_effect=lambda copy: fakes[copy["name"]])

    def test_each_backup_goes_to_both_copies(self):
        self.configure()
        b2, r2 = FakeRestic(self.module), FakeRestic(self.module)
        with self.app.app_context(), self.with_fakes(b2, r2):
            self.assertEqual([c["name"] for c in self.module.cloud_copies()], ["backblaze", "r2"])
            result = self.module.run_cloud_backup()
        self.assertTrue(result["ok"], result)
        self.assertEqual((len(b2.saved), len(r2.saved)), (1, 1))
        self.assertTrue(self.setting("cloud_backup_last_success"))
        self.assertTrue(self.setting("cloud_backup_r2_last_success"))
        self.assertEqual(self.setting("cloud_backup_r2_storage_bytes"), "4096")
        self.assertEqual(result["warning"], "")

    def test_one_copy_failing_does_not_stop_the_other(self):
        self.configure()
        b2, r2 = FakeRestic(self.module, fail="Backblaze keeps refusing one backup file (snapshot/x)."), FakeRestic(self.module)
        with self.app.app_context(), self.with_fakes(b2, r2):
            result = self.module.run_cloud_backup()
            # A failed copy is tried again soon, not at the next hourly slot.
            self.module.set_setting("cloud_backup_interval_hours", "1")
            soon = self.module.utc_now() + timedelta(minutes=self.module.CLOUD_BACKUP_RETRY_MINUTES + 1)
            self.assertTrue(self.module.cloud_backup_due(soon))
            status = self.module.cloud_backup_status()
        self.assertTrue(result["ok"], "the R2 copy was saved")
        self.assertEqual(len(r2.saved), 1)
        self.assertIn("Saved to Cloudflare R2, but not to Backblaze", result["warning"])
        self.assertIn("keeps refusing", self.setting("cloud_backup_last_error"))
        self.assertEqual(self.setting("cloud_backup_r2_last_error"), "")
        self.assertIn("keeps refusing", status["last_error"])
        self.assertTrue(status["r2"]["configured"] and status["r2"]["last_success"])

    def test_both_copies_failing(self):
        self.configure()
        b2, r2 = FakeRestic(self.module, fail="b2 down"), FakeRestic(self.module, fail="r2 down")
        with self.app.app_context(), self.with_fakes(b2, r2):
            result = self.module.run_cloud_backup()
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "Backblaze: b2 down Cloudflare R2: r2 down")

    def test_backblaze_only_works_as_before(self):
        self.configure(r2=False)
        b2 = FakeRestic(self.module, fail="upload cut off")
        with self.app.app_context(), self.with_fakes(b2, None):
            self.assertEqual([c["name"] for c in self.module.cloud_copies()], ["backblaze"])
            result = self.module.run_cloud_backup()
            self.assertFalse(self.module.cloud_backup_status()["r2"]["configured"])
        self.assertEqual(result, {"ok": False, "error": "upload cut off"})

    def test_alert_names_the_copy_that_stopped(self):
        self.configure()
        old = (datetime.now(timezone.utc) - timedelta(hours=10)).isoformat()
        with self.app.app_context():
            self.module.set_setting("cloud_backup_interval_hours", "1")
            self.module.set_setting("cloud_backup_last_success", datetime.now(timezone.utc).isoformat())
            self.module.set_setting("cloud_backup_r2_last_success", old)
            self.assertIn("Cloud backups to Cloudflare R2 have not worked for 10 hours", self.module.cloud_backup_alert())

    def test_connect_r2_from_settings(self):
        self.configure(r2=False)
        client = self.admin_client()
        form = {"csrf_token": "test-csrf", "cloud_backup_r2_endpoint": f"https://{ACCOUNT}.r2.cloudflarestorage.com/statement-backups",
                "cloud_backup_r2_key_id": R2_KEY_ID, "cloud_backup_r2_secret_key": R2_SECRET}
        with patch.object(cloud_backup.Restic, "exists", return_value=False), \
                patch.object(cloud_backup.Restic, "init") as init:
            response = client.post("/settings/cloud-backup/r2/save", data=form)
        self.assertEqual(response.status_code, 302)
        init.assert_called_once()
        self.assertEqual(self.setting("cloud_backup_r2_host"), f"{ACCOUNT}.r2.cloudflarestorage.com")
        self.assertEqual(self.setting("cloud_backup_r2_bucket"), "statement-backups")
        self.assertEqual(self.setting("cloud_backup_r2_prefix"), "statement-software")
        with self.app.app_context():
            self.assertTrue(self.module.r2_configured())
            log = " ".join(row[0] for row in self.module.get_db().execute("select detail from api_audit_log"))
        self.assertNotIn(R2_SECRET, log, "the secret never reaches the activity log")

        page = client.get("/settings").get_data(as_text=True)
        self.assertIn("Second copy in Cloudflare R2", page)
        self.assertNotIn(R2_SECRET, page, "the secret is never shown again")

        # Saving again without the secret keeps it; connecting to existing backups checks the key opens them.
        form.pop("cloud_backup_r2_secret_key")
        with patch.object(cloud_backup.Restic, "exists", return_value=True), \
                patch.object(cloud_backup.Restic, "snapshots", return_value=[]) as snapshots:
            client.post("/settings/cloud-backup/r2/save", data=form)
        snapshots.assert_called_once()
        self.assertEqual(self.setting("cloud_backup_r2_secret_key"), R2_SECRET)

        client.post("/settings/cloud-backup/r2/disconnect", data={"csrf_token": "test-csrf"})
        with self.app.app_context():
            self.assertFalse(self.module.r2_configured())
        self.assertEqual(self.setting("cloud_backup_r2_secret_key"), "")
        self.assertTrue(self.setting("cloud_backup_key_id"), "Backblaze stays connected")

    def test_r2_needs_the_main_backup_first(self):
        client = self.admin_client()
        response = client.post("/settings/cloud-backup/r2/save", data={
            "csrf_token": "test-csrf", "cloud_backup_r2_endpoint": f"https://{ACCOUNT}.r2.cloudflarestorage.com/statement-backups",
            "cloud_backup_r2_key_id": R2_KEY_ID, "cloud_backup_r2_secret_key": R2_SECRET})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.setting("cloud_backup_r2_host"), "")

    def test_turning_off_cloud_backup_forgets_r2_too(self):
        self.configure()
        self.admin_client().post("/settings/cloud-backup/disconnect", data={"csrf_token": "test-csrf"})
        self.assertEqual(self.setting("cloud_backup_r2_secret_key"), "")
        self.assertEqual(self.setting("cloud_backup_r2_host"), "")

    def test_list_from_the_chosen_copy(self):
        self.configure()
        b2, r2 = FakeRestic(self.module), FakeRestic(self.module)
        r2.saved.append("r2" * 32)
        with self.with_fakes(b2, r2):
            client = self.admin_client()
            from_r2 = client.get("/api/cloud-backup/snapshots?copy=r2").get_json()
            from_b2 = client.get("/api/cloud-backup/snapshots").get_json()
            unknown = client.get("/api/cloud-backup/snapshots?copy=nope").get_json()
        self.assertEqual([s["short_id"] for s in from_r2["snapshots"]], ["r2r2r2r2"])
        self.assertEqual(from_b2["snapshots"], [])
        self.assertEqual(unknown["snapshots"], [], "an unknown copy falls back to Backblaze")

    @unittest.skipUnless(HAVE_RESTIC, "restic not installed")
    def test_real_backup_to_both_then_restore_from_r2(self):
        self.configure()
        with self.app.app_context():
            for copy in self.module.cloud_copies():
                self.module.make_copy_restic(copy).init()
            db = self.module.get_db()
            db.execute("delete from clients")
            db.execute("insert into clients(name) values ('Before Backup')")
            db.commit()
            result = self.module.run_cloud_backup()
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["warning"], "")
            verify = self.module.run_cloud_verify()
            self.assertTrue(verify["ok"], verify)
            self.assertIn("both copies", verify["message"])
            r2_snapshot = self.module.make_r2_restic(self.module.load_r2_config()).snapshots()[0]["id"]
            db.execute("update clients set name = 'After Backup'")
            db.commit()
        client = self.admin_client()
        response = client.post("/settings/cloud-backup/restore",
                               data={"csrf_token": "test-csrf", "snapshot_id": r2_snapshot, "confirm_text": "RESTORE", "copy": "r2"})
        self.assertIn("/login", response.headers["Location"])
        with sqlite3.connect(self.module.DB_PATH) as conn:
            self.assertEqual([row[0] for row in conn.execute("select name from clients")], ["Before Backup"])


if __name__ == "__main__":
    unittest.main()
