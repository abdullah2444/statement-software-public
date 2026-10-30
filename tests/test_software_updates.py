"""The Software Updates card, the update check, update requests and pausing saves.

The host updater itself is tested in test_statement_updater.py.
"""

import importlib
import importlib.util
import io
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import unittest
import unittest.mock
from contextlib import redirect_stdout
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT))


def release(tag, body="Notes", draft=False, prerelease=False):
    return {
        "tag_name": tag, "name": tag, "body": body, "draft": draft, "prerelease": prerelease,
        "html_url": f"https://github.com/x/y/releases/tag/{tag}", "published_at": "2026-10-20T10:00:00Z",
        "tarball_url": f"https://api.github.com/repos/x/y/tarball/{tag}",
    }


class SoftwareUpdateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        base = Path(cls.tmp.name)
        (base / "data").mkdir()
        cls.env = patch.dict(os.environ, {
            "DATABASE_PATH": str(base / "data" / "test.db"),
            "UPLOAD_DIR": str(base / "data" / "uploads"),
            "BACKUP_DIR": str(base / "data" / "backups"),
            "UPDATER_DIR": str(base / "data" / "updater"),
            "CLOUD_BACKUP_REPOSITORY_OVERRIDE": str(base / "restic-repo"),
            "SECRET_KEY": "test-only", "INITIAL_ADMIN_USERNAME": "admin",
            "INITIAL_ADMIN_PASSWORD": "admin-pass-1",
        })
        cls.env.start()
        # Loaded as "app" so update_helper.py (which does `import app`) shares this instance.
        spec = importlib.util.spec_from_file_location("app", ROOT / "app.py")
        cls.module = importlib.util.module_from_spec(spec)
        sys.modules["app"] = cls.module
        spec.loader.exec_module(cls.module)
        cls.module.init_db()
        cls.app = cls.module.app
        cls.app.config["TESTING"] = True
        cls.channel = cls.module.UPDATER_DIR
        with cls.app.app_context():
            db = cls.module.get_db()
            db.execute(
                "insert into users(username, password_hash, role, must_change_password, created_at) values (?, ?, 'user', 0, ?)",
                ("clerk", cls.module.generate_password_hash("clerk-pass-1"), cls.module.utc_timestamp()),
            )
            db.commit()

    @classmethod
    def tearDownClass(cls):
        sys.modules.pop("app", None)
        sys.modules.pop("update_helper", None)
        cls.env.stop()
        cls.tmp.cleanup()

    def setUp(self):
        self.module.APP_VERSION = "5.1.0"
        if self.module._update_pause_held:
            (self.channel / "pause.json").unlink(missing_ok=True)
            self.module.sync_update_pause()
        for item in self.channel.glob("*") if self.channel.exists() else []:
            item.unlink()
        with self.app.app_context():
            db = self.module.get_db()
            db.execute("delete from app_settings where key like 'update_%'")
            db.commit()

    def login(self, client, username="admin"):
        with self.app.app_context():
            user_id = self.module.get_db().execute("select id from users where username = ?", (username,)).fetchone()["id"]
        with client.session_transaction() as sess:
            sess["_csrf_token"] = "test-csrf"
            sess["user_id"] = user_id
        return {"X-CSRF-Token": "test-csrf", "X-Requested-With": "XMLHttpRequest"}

    def github_answers(self, releases, status=200):
        response = unittest.mock.Mock(status_code=status)
        response.json.return_value = releases
        return patch.object(self.module.http_requests, "get", return_value=response)

    def check(self, releases):
        with self.github_answers(releases), self.app.app_context():
            return self.module.check_for_updates()

    def heartbeat(self, age_seconds=0):
        at = self.module.utc_now() - timedelta(seconds=age_seconds)
        self.module._write_updater_json("heartbeat.json", {"at": at.isoformat().replace("+00:00", "Z")})

    # ── Versions and checking ─────────────────────────────────────────────

    def test_version_parsing(self):
        self.assertEqual(self.module.parse_version("v5.10.2"), (5, 10, 2))
        self.assertEqual(self.module.parse_version("5.1.0"), (5, 1, 0))
        for bad in ["", "v5.1", "latest", "v5.1.0-rc1", "5.1.0; rm -rf /"]:
            self.assertIsNone(self.module.parse_version(bad), bad)

    def test_installed_version_comes_from_version_file(self):
        self.assertEqual(self.module._read_app_version(), (ROOT / "VERSION").read_text().strip())

    def test_check_lists_only_newer_published_releases_newest_first(self):
        result = self.check([
            release("v5.1.1", "Fixes"), release("v5.2.0", "New things"), release("v5.0.9"),
            release("v6.0.0", draft=True), release("v5.3.0", prerelease=True), release("nightly"),
            release("v5.1.0"),
        ])
        self.assertEqual(result, {"ok": True, "latest": "5.2.0", "newer": 2})
        with self.app.app_context():
            available = self.module.available_updates()
            self.assertEqual([r["version"] for r in available], ["5.2.0", "5.1.1"])
            self.assertEqual(available[0]["notes"], "New things")
            self.assertTrue(self.module.get_setting("update_last_check"))

    def test_check_failure_is_remembered_and_shown(self):
        with self.github_answers({}, status=503), self.app.app_context():
            result = self.module.check_for_updates()
            self.assertFalse(result["ok"])
            self.assertIn("503", self.module.get_setting("update_last_check_error"))
        with patch.object(self.module.http_requests, "get", side_effect=self.module.http_requests.ConnectionError()), \
                self.app.app_context():
            self.assertIn("Could not reach GitHub", self.module.check_for_updates()["error"])

    def test_up_to_date_after_installing_the_latest(self):
        self.check([release("v5.2.0")])
        self.module.APP_VERSION = "5.2.0"
        with self.app.app_context():
            self.assertEqual(self.module.available_updates(), [])

    # ── Page, banner and permissions ─────────────────────────────────────

    def test_settings_card_shows_versions_notes_and_history(self):
        self.check([release("v5.2.0", "Faster <b>exports</b>")])
        self.heartbeat()
        self.module._write_updater_json("history.json", [{
            "target": "v5.1.0", "from_version": "5.0.0", "result": "rolled_back",
            "message": "The new version didn't start.", "finished_at": "2026-10-19T09:00:00Z",
        }])
        client = self.app.test_client()
        self.login(client)
        page = client.get("/settings").get_data(as_text=True)
        self.assertIn('id="software-updates"', page)
        self.assertIn('Version <strong id="upd-installed">5.1.0</strong>', page)
        self.assertIn("Update now to 5.2.0", page)
        self.assertIn("Faster &lt;b&gt;exports&lt;/b&gt;", page)  # release notes are shown as text
        self.assertIn("Put back", page)
        self.assertIn("Version 5.2.0 of the software is available.", page)  # banner

    def test_banner_only_for_admins_until_an_update_runs(self):
        self.check([release("v5.2.0")])
        clerk = self.app.test_client()
        self.login(clerk, "clerk")
        self.assertNotIn("of the software is available", clerk.get("/").get_data(as_text=True))
        self.module._write_updater_json("pause.json", {})
        self.assertIn("Saving is paused", clerk.get("/").get_data(as_text=True))

    def test_ordinary_users_cannot_see_or_start_updates(self):
        self.check([release("v5.2.0")])
        self.heartbeat()
        client = self.app.test_client()
        headers = self.login(client, "clerk")
        self.assertEqual(client.get("/api/updates/status").status_code, 403)
        self.assertEqual(client.post("/settings/updates/check", headers=headers).status_code, 403)
        self.assertEqual(client.post("/settings/updates/install", data={"version": "5.2.0"}, headers=headers).status_code, 403)
        self.assertFalse((self.channel / "request.json").exists())

    def test_logged_out_requests_are_refused(self):
        client = self.app.test_client()
        response = client.post("/settings/updates/install", data={"version": "5.2.0"})
        self.assertIn(response.status_code, (302, 400, 401))
        self.assertFalse((self.channel / "request.json").exists())

    def test_update_requests_need_the_csrf_token(self):
        self.check([release("v5.2.0")])
        self.heartbeat()
        client = self.app.test_client()
        self.login(client)
        response = client.post("/settings/updates/install", data={"version": "5.2.0"}, headers={"X-Requested-With": "XMLHttpRequest"})
        self.assertEqual(response.status_code, 400)
        self.assertFalse((self.channel / "request.json").exists())

    def test_healthz_is_public_and_reports_version(self):
        response = self.app.test_client().get("/healthz")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"ok": True, "version": "5.1.0"})

    # ── Requesting an update ─────────────────────────────────────────────

    def test_update_now_writes_a_request_for_the_updater(self):
        self.check([release("v5.2.0")])
        self.heartbeat()
        client = self.app.test_client()
        headers = self.login(client)
        response = client.post("/settings/updates/install", data={"version": "5.2.0"}, headers=headers)
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        request = json.loads((self.channel / "request.json").read_text())
        self.assertEqual(request["target"], "v5.2.0")
        self.assertEqual(request["from_version"], "5.1.0")
        self.assertEqual(request["requested_by"], "admin")
        status = client.get("/api/updates/status").get_json()
        self.assertTrue(status["in_progress"])
        # A second click while one is waiting is refused.
        again = client.post("/settings/updates/install", data={"version": "5.2.0"}, headers=headers)
        self.assertEqual(again.status_code, 409)

    def test_only_a_listed_release_can_be_requested(self):
        self.check([release("v5.2.0")])
        self.heartbeat()
        client = self.app.test_client()
        headers = self.login(client)
        for version in ["5.3.0", "5.0.0", "../../etc", ""]:
            response = client.post("/settings/updates/install", data={"version": version}, headers=headers)
            self.assertEqual(response.status_code, 400, version)
        self.assertFalse((self.channel / "request.json").exists())

    def test_update_refused_when_updater_is_not_running(self):
        self.check([release("v5.2.0")])
        client = self.app.test_client()
        headers = self.login(client)
        self.assertEqual(client.post("/settings/updates/install", data={"version": "5.2.0"}, headers=headers).status_code, 409)
        self.heartbeat(age_seconds=600)
        self.assertEqual(client.post("/settings/updates/install", data={"version": "5.2.0"}, headers=headers).status_code, 409)
        self.assertFalse((self.channel / "request.json").exists())

    # ── Pausing saves ────────────────────────────────────────────────────

    def test_saving_is_refused_while_paused_but_pages_still_load(self):
        client = self.app.test_client()
        headers = self.login(client)
        self.module._write_updater_json("pause.json", {})
        response = client.post("/clients/new", data={"name": "During update"}, headers=headers)
        self.assertEqual(response.status_code, 503)
        self.assertIn("being updated", response.get_json()["error"])
        self.assertEqual(client.get("/").status_code, 200)
        (self.channel / "pause.json").unlink()
        self.assertNotEqual(client.post("/clients/new", data={"name": "After update"}, headers=headers).status_code, 503)

    def test_people_can_still_log_in_while_paused(self):
        self.module._write_updater_json("pause.json", {})
        self.module.sync_update_pause()
        client = self.app.test_client()
        with client.session_transaction() as sess:
            sess["_csrf_token"] = "test-csrf"
        response = client.post("/login", data={"username": "clerk", "password": "clerk-pass-1", "csrf_token": "test-csrf"})
        self.assertEqual(response.status_code, 302)
        self.assertIn("Saving is paused", client.get("/").get_data(as_text=True))

    def test_pause_waits_for_a_save_in_progress_then_confirms(self):
        gate = self.module.DATA_GATE
        started, release_save = threading.Event(), threading.Event()

        def slow_save():
            gate.acquire_shared(timeout=5)
            started.set()
            release_save.wait(30)
            gate.release_shared()

        saver = threading.Thread(target=slow_save)
        saver.start()
        started.wait(5)
        self.module._write_updater_json("pause.json", {})
        self.module.sync_update_pause()  # gives up after a few seconds while the save is still running
        self.assertFalse((self.channel / "pause-ack.json").exists())
        release_save.set()
        saver.join(5)
        self.module.sync_update_pause()
        self.assertTrue((self.channel / "pause-ack.json").exists())
        blocked = []
        other = threading.Thread(target=lambda: blocked.append(not gate.acquire_shared(timeout=0.2)))
        other.start()
        other.join(5)
        self.assertEqual(blocked, [True], "saves from other requests must be held while paused")
        (self.channel / "pause.json").unlink()
        self.module.sync_update_pause()
        self.assertFalse((self.channel / "pause-ack.json").exists())
        self.assertTrue(gate.acquire_shared(timeout=1))
        gate.release_shared()

    def test_resume_only_when_the_updater_has_stopped(self):
        client = self.app.test_client()
        headers = self.login(client)
        self.module._write_updater_json("pause.json", {})
        self.heartbeat()
        self.assertEqual(client.post("/settings/updates/resume", headers=headers).status_code, 409)
        self.assertTrue((self.channel / "pause.json").exists())
        self.heartbeat(age_seconds=600)
        self.assertEqual(client.post("/settings/updates/resume", headers=headers).status_code, 200)
        self.assertFalse((self.channel / "pause.json").exists())

    # ── Helper run inside the container ──────────────────────────────────

    def test_recovery_backup_is_a_verified_full_backup(self):
        helper = importlib.import_module("update_helper")
        uploads = self.module.UPLOAD_DIR
        uploads.mkdir(parents=True, exist_ok=True)
        (uploads / "receipt.png").write_bytes(b"receipt")
        result = helper.backup("before-update-to-5.2.0")
        path = Path(result["path"])
        self.assertTrue(path.name.startswith("statement-full-backup-"))
        self.assertIn("before-update-to-5.2.0", path.name)
        self.assertEqual(result["upload_file_count"], 1)
        self.assertEqual(result["sha256"], helper._sha256(path))
        with self.app.app_context():
            self.assertIn(path.name, [b["name"] for b in self.module.list_database_backups()])

    @unittest.skipUnless(shutil.which("restic"), "restic is not installed")
    def test_recovery_backup_also_makes_a_cloud_backup_when_set_up(self):
        helper = importlib.import_module("update_helper")
        settings = {
            "cloud_backup_region": "us-west-004", "cloud_backup_bucket": "test-bucket-123",
            "cloud_backup_prefix": "statement-software", "cloud_backup_key_id": "0040000000000000000000001",
            "cloud_backup_app_key": "K004FAKEAPPLICATIONKEYVALUE12345",
            "cloud_backup_recovery_key": "AAAA-BBBB-CCCC-DDDD-EEEE-FFFF-0000-1111",
        }
        with self.app.app_context():
            for key, value in settings.items():
                self.module.set_setting(key, value)
            self.module.set_setting("cloud_backup_instance", self.module.this_instance_id())
            restic = self.module.make_restic(self.module.load_cloud_backup_config())
            if not restic.exists():
                restic.init()
        try:
            result = helper.backup("before-update-to-5.2.0")
            self.assertTrue(result["cloud_snapshot"])
            with self.app.app_context():
                self.assertTrue(self.module.get_setting("cloud_backup_last_success"))
        finally:
            with self.app.app_context():
                db = self.module.get_db()
                db.execute("delete from app_settings where key like 'cloud_backup_%'")
                db.commit()

    def test_recovery_backup_goes_on_when_only_the_cloud_backup_fails(self):
        helper = importlib.import_module("update_helper")
        before = set(self.module.BACKUP_DIR.glob("*.tar.gz"))
        with patch.object(self.module, "cloud_backup_active", return_value=True), \
                patch.object(self.module, "run_cloud_backup", return_value={"ok": False, "error": "Backblaze keeps refusing one backup file."}):
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(helper.main(["update_helper.py", "backup", "x"]), 0)
        result = json.loads(out.getvalue())
        self.assertEqual(result["cloud_snapshot"], "")
        self.assertIn("only the local backup was made", result["cloud_warning"])
        made = set(self.module.BACKUP_DIR.glob("*.tar.gz")) - before
        self.assertEqual([path.name for path in made], [result["name"]])
        for path in made:
            path.unlink()

        # A scheduled cloud backup that never finishes doesn't hold the update up either.
        with patch.object(self.module, "cloud_backup_active", return_value=True), \
                patch.object(helper, "_wait_for_cloud_job", side_effect=RuntimeError("A cloud backup has been running for too long.")):
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(helper.main(["update_helper.py", "backup", "x"]), 0)
        self.assertIn("running for too long", json.loads(out.getvalue())["cloud_warning"])
        for path in set(self.module.BACKUP_DIR.glob("*.tar.gz")) - before:
            path.unlink()

    def test_damaged_recovery_backup_is_rejected(self):
        helper = importlib.import_module("update_helper")
        broken = self.module.BACKUP_DIR / "broken.tar.gz"
        broken.write_bytes(b"not a tarball")
        with self.assertRaises(ValueError):
            helper.verify_full_backup(broken)

    def test_checkpoint_command(self):
        helper = importlib.import_module("update_helper")
        self.assertEqual(helper.main(["update_helper.py", "checkpoint", "-"]), 0)


if __name__ == "__main__":
    unittest.main()
