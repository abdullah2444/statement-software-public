"""The host updater: success, failed download, failed backup, rollback, and the safety checks.

Docker and GitHub are replaced by FakeHost, which keeps a pretend container,
pretend images and a real SQLite database in a temporary folder, so every
step that touches data (backup, restore) runs for real.
"""

import io
import json
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "updater"))

import statement_updater as su  # noqa: E402

REAL_SLEEP = time.sleep


def completed(args, code=0, out="", err=""):
    return subprocess.CompletedProcess(args, code, out, err)


class FakeHost(su.Host):
    """A pretend Docker server with one app container started by Docker Compose."""

    def __init__(self, base: Path, channel: Path):
        self.base = base
        self.channel = channel
        self.data = base / "data"
        self.images = {"img-old": {"version": "5.1.0", "helper": True, "broken": False}}
        self.tags = {"statement-software:v5.0": "img-old"}
        self.running_image = "img-old"
        self.state = "running"
        self.restarts = 0
        self.compose_env = {"SECRET_KEY": "s3cret", "PORT": "18451"}
        self.compose_image = "statement-software:v5.0"
        self.container_env = dict(self.compose_env)
        self.releases = {}
        self.download_error = None
        self.build_error = False
        self.backup_error = ""
        self.fail_compose_up_after = None
        self.compose_up_calls = 0
        self.free = 50 * 1024 ** 3
        self.commands = []
        self._stop = threading.Event()
        threading.Thread(target=self._app_watches_pause, daemon=True).start()

    def close(self):
        self._stop.set()

    # The real app writes pause-ack.json once it has paused saving.
    def _app_watches_pause(self):
        while not self._stop.is_set():
            image = self.images.get(self.running_image, {})
            if self.state == "running" and image.get("helper") and (self.channel / "pause.json").exists():
                if not (self.channel / "pause-ack.json").exists():
                    (self.channel / "pause-ack.json").write_text("{}")
            REAL_SLEEP(0.02)

    @property
    def version(self):
        return self.images[self.running_image]["version"]

    def inspect_json(self):
        return [{
            "Image": self.running_image,
            "Config": {
                "Image": "statement-software:v5.0",
                "Env": [f"{k}={v}" for k, v in self.container_env.items()] + [
                    "DATABASE_PATH=/data/firefly_statement.db", "UPLOAD_DIR=/data/uploads", "BACKUP_DIR=/data/backups",
                ],
                "Labels": {
                    "com.docker.compose.project": "statement",
                    "com.docker.compose.project.working_dir": str(self.base / "compose"),
                    "com.docker.compose.project.config_files": str(self.base / "compose" / "docker-compose.yml"),
                    "com.docker.compose.service": "statement-software-v5",
                },
            },
            "Mounts": [
                {"Source": str(self.data / "firefly_statement.db"), "Destination": "/data/firefly_statement.db"},
                {"Source": str(self.data / "uploads"), "Destination": "/data/uploads"},
                {"Source": str(self.data / "backups"), "Destination": "/data/backups"},
                {"Source": str(self.channel), "Destination": "/data/updater"},
            ],
        }]

    def run(self, args, timeout=600, check=True):
        self.commands.append(args)
        result = self._dispatch(list(args))
        if check and result.returncode != 0:
            raise su.UpdateError(f"Command failed: {' '.join(args[:3])}: {result.stderr}")
        return result

    def _dispatch(self, args):
        if args[:2] == ["docker", "inspect"] and args[2] == "-f":
            restarts = self.restarts if "RestartCount" in args[3] else None
            return completed(args, out=self.state + ("" if restarts is None else f" {restarts}") + "\n")
        if args[:2] == ["docker", "inspect"]:
            return completed(args, out=json.dumps(self.inspect_json()))
        if args[:3] == ["docker", "compose", "version"]:
            return completed(args, out="Docker Compose version v2.29.0")
        if args[:2] == ["docker", "compose"]:
            return self._compose(args)
        if args[:2] == ["docker", "build"]:
            if self.build_error:
                return completed(args, 1, err="pip install failed")
            image = args[args.index("-t") + 1]
            source = Path(args[-1])
            version = (source / "VERSION").read_text().strip()
            image_id = f"img-{version}"
            self.images[image_id] = {"version": version, "helper": True, "broken": (source / "BROKEN").exists()}
            self.tags[image] = image_id
            return completed(args)
        if args[:3] == ["docker", "image", "inspect"]:
            return completed(args, out=self.tags[args[-1]] + "\n")
        if args[:2] == ["docker", "tag"]:
            source = self.tags.get(args[2], args[2])
            self.tags[args[3]] = source
            return completed(args)
        if args[:2] == ["docker", "stop"]:
            self.state = "exited"
            return completed(args)
        if args[:2] == ["docker", "exec"]:
            return self._exec(args[3:])
        return completed(args, 1, err=f"unexpected command {args}")

    def _compose(self, args):
        if "config" in args:
            service = {"environment": self.compose_env, "image": self.compose_image}
            return completed(args, out=json.dumps({"services": {"statement-software-v5": service}}))
        if "up" in args:
            self.compose_up_calls += 1
            if self.fail_compose_up_after is not None and self.compose_up_calls > self.fail_compose_up_after:
                return completed(args, 1, err="port is already allocated")
            self.running_image = self.tags[self.compose_image]
            self.state = "running"
            self.restarts = 5 if self.images[self.running_image]["broken"] else 0
            image = self.images[self.running_image]
            if self.running_image != "img-old":
                # A new version upgrades the database when it starts.
                with sqlite3.connect(self.data / "firefly_statement.db") as db:
                    db.execute("create table if not exists new_feature(x)")
                    db.execute("update clients set name = 'changed by ' || ?", (image["version"],))
            return completed(args)
        return completed(args, 1, err="unexpected compose command")

    def _exec(self, args):
        if self.state != "running":
            return completed(args, 1, err="container is not running")
        image = self.images[self.running_image]
        if args[:2] == ["cat", "VERSION"]:
            return completed(args, out=image["version"] + "\n")
        if args[:2] == ["test", "-f"]:
            return completed(args, 0 if image["helper"] else 1)
        if args[:2] == ["python", "-c"]:
            if image["broken"]:
                return completed(args, 1, err="Connection refused")
            return completed(args, out=json.dumps({"ok": True, "version": image["version"]}))
        if args[:2] == ["python", "update_helper.py"]:
            return self._helper(args[2], args[3], image)
        return completed(args, 1, err="unexpected exec")

    def _helper(self, command, argument, image):
        if command == "checkpoint":
            return completed([], out='{"ok": true}')
        if command == "health":
            if image["broken"] or argument.lstrip("v") != image["version"]:
                return completed([], 1, out=json.dumps({"ok": False, "error": "wrong version"}))
            return completed([], out='{"ok": true}')
        if command == "backup":
            if self.backup_error:
                return completed([], 1, out=json.dumps({"ok": False, "error": self.backup_error}))
            name = f"statement-full-backup-test-{argument}.tar.gz"
            target = self.data / "backups" / name
            su.make_cold_backup(self.data / "firefly_statement.db", self.data / "uploads", target)
            count = sum(1 for p in (self.data / "uploads").rglob("*") if p.is_file())
            return completed([], out=json.dumps({
                "ok": True, "name": name, "path": f"/data/backups/{name}", "size": target.stat().st_size,
                "sha256": su.sha256(target), "upload_file_count": count, "cloud_snapshot": "",
            }))
        return completed([], 1, err="unknown helper command")

    def http_json(self, url):
        tag = url.rsplit("/", 1)[-1]
        if tag not in self.releases:
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
        return self.releases[tag]["json"]

    def download(self, url, target):
        if self.download_error:
            raise self.download_error
        tag = url.rsplit("/", 1)[-1]
        target.write_bytes(self.releases[tag]["tarball"])

    def free_bytes(self, path):
        return self.free


def make_tarball(files: dict, top="abdullah2444-statement-software-public-abc1234") -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name, content in files.items():
            data = content.encode()
            info = tarfile.TarInfo(f"{top}/{name}" if top else name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


class UpdaterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.channel = base / "data" / "updater"
        (base / "data" / "uploads").mkdir(parents=True)
        (base / "data" / "backups").mkdir()
        (base / "compose").mkdir()
        self.channel.mkdir()
        self.db_path = base / "data" / "firefly_statement.db"
        with sqlite3.connect(self.db_path) as db:
            db.execute("create table users(id integer primary key, username text)")
            db.execute("create table app_settings(key text primary key, value text)")
            db.execute("create table clients(id integer primary key, name text)")
            db.execute("insert into users(username) values ('admin')")
            db.execute("insert into clients(name) values ('Original client')")
        (base / "data" / "uploads" / "receipt.png").write_bytes(b"receipt")
        self.host = FakeHost(base, self.channel)
        self.config = su.Config(container="statement-software-v5", channel=self.channel, work=base / "work",
                                health_timeout=1, pause_timeout=2, poll_seconds=0.01)
        self.updater = su.Updater(self.config, self.host)
        self.add_release("v5.2.0")
        self.sleep = patch.object(su.time, "sleep", lambda s: REAL_SLEEP(min(s, 0.01)))
        self.sleep.start()

    def tearDown(self):
        self.sleep.stop()
        self.host.close()
        self.tmp.cleanup()

    def add_release(self, tag, broken=False, files=None, draft=False, prerelease=False):
        files = files or {"VERSION": tag.lstrip("v") + "\n", "Dockerfile": "FROM python", "app.py": "", "update_helper.py": ""}
        if broken:
            files["BROKEN"] = ""
        self.host.releases[tag] = {
            "json": {"tag_name": tag, "draft": draft, "prerelease": prerelease,
                     "tarball_url": f"https://api.github.com/repos/x/y/tarball/{tag}"},
            "tarball": make_tarball(files),
        }

    def update(self, tag="v5.2.0", **extra):
        return self.updater.process({"id": "test-run", "target": tag, "requested_by": "admin", **extra})

    def status(self):
        return json.loads((self.channel / "status.json").read_text())

    def history(self):
        return json.loads((self.channel / "history.json").read_text())

    def client_names(self):
        with sqlite3.connect(self.db_path) as db:
            return [r[0] for r in db.execute("select name from clients")]

    def tables(self):
        with sqlite3.connect(self.db_path) as db:
            return {r[0] for r in db.execute("select name from sqlite_master where type='table'")}

    def assert_untouched(self):
        self.assertEqual(self.host.running_image, "img-old")
        self.assertEqual(self.host.tags["statement-software:v5.0"], "img-old")
        self.assertEqual(self.host.compose_up_calls, 0)
        self.assertEqual(self.host.state, "running")
        self.assertFalse((self.channel / "pause.json").exists(), "saving must be back on")

    # ── The four rehearsal cases ────────────────────────────────────────

    def test_update_uses_image_name_from_compose(self):
        # install-updater.sh renames the image to "current", so it no longer says v5.0 after an update.
        self.host.compose_image = "statement-software:current"
        self.assertEqual(self.update(), "installed")
        self.assertEqual(self.host.version, "5.2.0")
        self.assertEqual(self.host.tags["statement-software:current"], "img-5.2.0")

    def test_rollback_uses_image_name_from_compose(self):
        self.host.compose_image = "statement-software:current"
        self.add_release("v5.2.0", broken=True)
        self.assertEqual(self.update(), "rolled_back")
        self.assertEqual(self.host.running_image, "img-old")
        self.assertEqual(self.host.tags["statement-software:current"], "img-old")

    def test_successful_update(self):
        self.assertEqual(self.update(), "installed")
        self.assertEqual(self.host.version, "5.2.0")
        self.assertEqual(self.host.tags["statement-software-release:previous"], "img-old")
        self.assertFalse((self.channel / "pause.json").exists())
        status = self.status()
        self.assertEqual((status["stage"], status["state"], status["result"]), ("complete", "done", "installed"))
        self.assertIn("Updated from 5.1.0 to 5.2.0", status["message"])
        entry = self.history()[0]
        self.assertEqual((entry["from_version"], entry["target"], entry["requested_by"]), ("5.1.0", "v5.2.0", "admin"))
        backups = list((self.host.data / "backups").glob("*before-update-to-5.2.0*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(entry["backup"], backups[0].name)
        self.assertTrue((self.channel / "logs" / "test-run.log").exists())
        # The backup was made after saving was paused, and before the restart.
        order = [c for c in self.host.commands if "backup" in c or "up" in c]
        self.assertIn("backup", order[0])

    def test_failed_download_changes_nothing(self):
        self.host.download_error = urllib.error.URLError("connection reset")
        self.assertEqual(self.update(), "not_started")
        self.assert_untouched()
        self.assertIn("Downloading v5.2.0 failed", self.status()["message"])
        self.assertIn("Nothing was changed", self.status()["message"])
        self.assertEqual(list((self.host.data / "backups").iterdir()), [])
        self.assertFalse(list((self.config.work / "downloads").glob("*.part")))

    def test_download_cut_off_halfway_changes_nothing(self):
        import http.client
        self.host.download_error = http.client.IncompleteRead(b"partial", 1000)
        self.assertEqual(self.update(), "not_started")
        self.assert_untouched()
        self.assertIn("Downloading v5.2.0 failed", self.status()["message"])

    def test_failed_backup_changes_nothing(self):
        self.host.backup_error = "Cloud backup failed: Backblaze refused the key."
        self.assertEqual(self.update(), "not_started")
        self.assert_untouched()
        self.assertIn("Backblaze refused the key", self.status()["message"])
        self.assertEqual(self.history()[0]["result"], "not_started")

    def test_failed_check_rolls_back_version_and_data(self):
        self.add_release("v5.2.0", broken=True)
        self.assertEqual(self.update(), "rolled_back")
        self.assertEqual(self.host.version, "5.1.0")
        self.assertEqual(self.host.tags["statement-software:v5.0"], "img-old")
        self.assertEqual(self.host.state, "running")
        # The broken version changed the database when it started; that change is undone.
        self.assertEqual(self.client_names(), ["Original client"])
        self.assertNotIn("new_feature", self.tables())
        self.assertTrue((self.host.data / "uploads" / "receipt.png").exists())
        self.assertFalse((self.channel / "pause.json").exists())
        status = self.status()
        self.assertEqual((status["state"], status["result"]), ("failed", "rolled_back"))
        self.assertIn("previous version and your data were put back", status["message"])

    def test_crashing_new_version_is_noticed_without_waiting_for_the_timeout(self):
        self.add_release("v5.2.0", broken=True)
        self.config.health_timeout = 3600
        started = time.monotonic()
        self.assertEqual(self.update(), "rolled_back")
        self.assertLess(time.monotonic() - started, 30)
        self.assertIn("keeps stopping", self.status()["message"])

    def test_failed_rollback_keeps_saving_paused(self):
        self.add_release("v5.2.0", broken=True)
        self.host.fail_compose_up_after = 1
        self.assertEqual(self.update(), "rollback_failed")
        self.assertTrue((self.channel / "pause.json").exists(), "nobody may save until a person has looked")
        self.assertIn("Saving stays paused", self.status()["message"])
        self.assertIn("before-update-to-5.2.0", self.status()["message"])

    # ── Safety checks before anything changes ───────────────────────────

    def test_only_published_releases_are_installed(self):
        self.assertEqual(self.update("v9.9.9"), "not_started")
        self.assertIn("not published", self.status()["message"])
        self.add_release("v5.3.0", draft=True)
        self.assertEqual(self.update("v5.3.0"), "not_started")
        self.add_release("v5.4.0", prerelease=True)
        self.assertEqual(self.update("v5.4.0"), "not_started")
        self.assertEqual(self.update("main; rm -rf /"), "not_started")
        self.assert_untouched()

    def test_same_or_older_version_is_refused(self):
        self.add_release("v5.1.0")
        self.add_release("v5.0.9")
        self.assertEqual(self.update("v5.1.0"), "not_started")
        self.assertEqual(self.update("v5.0.9"), "not_started")
        # A request file can't ask for a downgrade; only the command line can.
        self.assertEqual(self.update("v5.0.9", allow_downgrade=True), "not_started")
        self.assert_untouched()

    def test_not_enough_disk_space(self):
        self.host.free = 10 * 1024 * 1024
        self.assertEqual(self.update(), "not_started")
        self.assertIn("Not enough free disk space", self.status()["message"])
        self.assert_untouched()

    def test_failed_build(self):
        self.host.build_error = True
        self.assertEqual(self.update(), "not_started")
        self.assert_untouched()

    def test_compose_settings_that_would_change_are_named_not_shown(self):
        self.host.compose_env = {"SECRET_KEY": "", "PORT": "18451"}
        self.assertEqual(self.update(), "not_started")
        message = self.status()["message"]
        self.assertIn("SECRET_KEY", message)
        self.assertNotIn("s3cret", message)
        self.assert_untouched()

    def test_app_that_does_not_pause_in_time(self):
        self.host.images["img-old"]["helper"] = True
        with patch.object(self.host, "_app_watches_pause", lambda: None):
            host = self.host
            host._stop.set()  # stop the pretend app from confirming the pause
            REAL_SLEEP(0.05)
            self.assertEqual(self.update(), "not_started")
        self.assertIn("did not pause saving", self.status()["message"])
        self.assert_untouched()

    def test_download_must_match_the_release(self):
        self.add_release("v5.2.0", files={"VERSION": "5.3.0", "Dockerfile": "", "app.py": "", "update_helper.py": ""})
        self.assertEqual(self.update(), "not_started")
        self.assertIn("says it is version 5.3.0", self.status()["message"])
        self.add_release("v5.2.0", files={"VERSION": "5.2.0", "Dockerfile": ""})
        self.assertEqual(self.update(), "not_started")
        self.assertIn("incomplete", self.status()["message"])

    def test_unsafe_paths_in_download_are_refused(self):
        archive = Path(self.tmp.name) / "evil.tar.gz"
        for name in ["top/../../escape.txt", "/etc/passwd"]:
            archive.write_bytes(make_tarball({name: "x"}, top=""))
            with self.assertRaises(su.UpdateError):
                su.extract_release(archive, Path(self.tmp.name) / "out")
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
            link = tarfile.TarInfo("top/link")
            link.type = tarfile.SYMTYPE
            link.linkname = "/etc/passwd"
            tar.addfile(link)
        archive.write_bytes(buffer.getvalue())
        with self.assertRaises(su.UpdateError):
            su.extract_release(archive, Path(self.tmp.name) / "out")
        self.assertFalse((Path(self.tmp.name).parent / "escape.txt").exists())

    # ── First install and interruptions ─────────────────────────────────

    def test_first_install_over_a_version_without_the_updater(self):
        self.host.images["img-old"].update({"helper": False, "version": "5.0.0"})
        self.host.running_image = "img-old"
        self.assertEqual(self.update(), "installed")
        self.assertEqual(self.host.version, "5.2.0")
        self.assertTrue(list((self.host.data / "backups").glob("*before-update-to-5.2.0*")))

    def test_first_install_failure_puts_old_version_back(self):
        self.host.images["img-old"].update({"helper": False, "version": "5.0.0"})
        self.add_release("v5.2.0", broken=True)
        self.assertEqual(self.update(), "rolled_back")
        self.assertEqual(self.host.version, "5.0.0")
        self.assertEqual(self.client_names(), ["Original client"])

    def test_interrupted_update_is_rolled_back_when_the_updater_restarts(self):
        backup = self.host.data / "backups" / "statement-full-backup-interrupted.tar.gz"
        su.make_cold_backup(self.db_path, self.host.data / "uploads", backup)
        self.host.images["img-5.2.0"] = {"version": "5.2.0", "helper": True, "broken": True}
        self.host.tags["statement-software:v5.0"] = "img-5.2.0"
        self.host._compose(["up"])
        su.write_json(self.channel / "pause.json", {})
        su.write_json(self.channel / "status.json", {
            "id": "crashed", "target": "v5.2.0", "from_version": "5.1.0", "stage": "installing", "state": "running",
            "old_image": "img-old", "backup_path": str(backup), "backup": backup.name, "started_at": "2026-10-18T00:00:00Z",
        })
        self.updater.recover_interrupted()
        self.assertEqual(self.host.version, "5.1.0")
        self.assertEqual(self.client_names(), ["Original client"])
        self.assertEqual(self.status()["result"], "rolled_back")
        self.assertFalse((self.channel / "pause.json").exists())

    def test_interrupted_before_install_just_resumes_saving(self):
        su.write_json(self.channel / "pause.json", {})
        su.write_json(self.channel / "status.json", {"id": "x", "stage": "backing_up", "state": "running", "target": "v5.2.0"})
        self.updater.recover_interrupted()
        self.assertEqual(self.status()["result"], "not_started")
        self.assertFalse((self.channel / "pause.json").exists())
        self.assert_untouched()

    def test_requests_are_taken_once(self):
        su.write_json(self.channel / "request.json", {"id": "r1", "target": "v5.2.0"})
        self.assertEqual(self.updater.take_request()["id"], "r1")
        self.assertIsNone(self.updater.take_request())

    def test_heartbeat(self):
        self.updater.heartbeat()
        beat = json.loads((self.channel / "heartbeat.json").read_text())
        self.assertEqual(beat["updater_version"], su.UPDATER_VERSION)

    def test_cold_backup_matches_the_app_format(self):
        target = Path(self.tmp.name) / "backup.tar.gz"
        su.make_cold_backup(self.db_path, self.host.data / "uploads", target)
        with tarfile.open(target) as tar:
            names = set(tar.getnames())
            manifest = json.load(tar.extractfile("manifest.json"))
        self.assertTrue({"manifest.json", "firefly_statement.db", "uploads/receipt.png"} <= names)
        self.assertEqual((manifest["format_version"], manifest["upload_file_count"]), (1, 1))


if __name__ == "__main__":
    unittest.main()
