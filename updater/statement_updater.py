#!/usr/bin/env python3
"""Statement Software host updater.

Runs as a service on the Docker server (not inside the app), so it keeps
working while the app restarts. It talks to the app only through files in a
shared folder (the "channel"), which the app sees as /data/updater:

    heartbeat.json   written here every few seconds, so the app knows we're alive
    request.json     written by the app when an admin clicks Update Now
    status.json      written here: progress of the current or last update
    history.json     written here: every update attempt, newest first
    pause.json       written here: the app holds all saving while it exists
    pause-ack.json   written by the app once saving is held
    logs/<id>.log    a detailed log of each update

An update goes through these stages:

    preparing    check the release on GitHub, check disk space, download it and
                 build the new image (the app keeps running normally)
    backing_up   pause saving, then make and verify a recovery backup
    installing   restart the app on the new image
    checking     wait until the new version answers and its database is fine
    complete     turn saving back on

If installing or checking fails, the previous image and the database from
the recovery backup are put back before saving is turned back on. Nothing
here needs a GitHub token: only published releases of a public repository
are installed. Only the Python standard library is used.

Usage:
    statement_updater.py run                 run as a service (systemd starts this)
    statement_updater.py install v5.1.1      install one release now, from the command line
    statement_updater.py status              print the last status
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import http.client
import json
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

UPDATER_VERSION = "1"
TAG_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
MAX_DOWNLOAD_BYTES = 200 * 1024 * 1024
MAX_EXTRACTED_BYTES = 400 * 1024 * 1024
REQUIRED_RELEASE_FILES = ("VERSION", "Dockerfile", "app.py", "update_helper.py")
RELEASE_IMAGE = "statement-software-release"
HISTORY_LIMIT = 100
KEEP_RELEASES = 3
STAGES = ("preparing", "backing_up", "installing", "checking", "complete")
CRASH_RESTARTS = 3

# Runs inside the container. Works on old versions too: they have no /healthz,
# so the login page answering is the best check available for them.
HEALTH_PROBE = """
import json, os, urllib.request, urllib.error
port = os.environ.get("PORT", "5050")
try:
    with urllib.request.urlopen("http://127.0.0.1:%s/healthz" % port, timeout=10) as r:
        print(json.dumps(json.loads(r.read().decode())))
except urllib.error.HTTPError as e:
    if e.code != 404:
        raise
    with urllib.request.urlopen("http://127.0.0.1:%s/login" % port, timeout=10) as r:
        print(json.dumps({"ok": r.status == 200, "version": ""}))
"""


class UpdateError(Exception):
    """A failure with a message meant for the person who asked for the update."""


class AppCrashing(UpdateError):
    pass


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_tag(tag: str) -> tuple[int, int, int]:
    match = TAG_RE.match(tag or "")
    if not match:
        raise UpdateError(f"'{tag}' is not a release name like v5.1.0.")
    return tuple(int(x) for x in match.groups())


def parse_version(value: str) -> tuple[int, int, int]:
    match = re.match(r"^v?(\d+)\.(\d+)\.(\d+)$", (value or "").strip())
    return tuple(int(x) for x in match.groups()) if match else (0, 0, 0)


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


@dataclass
class Config:
    container: str
    channel: Path
    work: Path
    repository: str = "abdullah2444/statement-software-public"
    api_base: str = "https://api.github.com"
    build_args: list[str] = field(default_factory=list)
    extra_compose_files: list[str] = field(default_factory=list)
    health_timeout: int = 180
    pause_timeout: int = 120
    poll_seconds: float = 3.0

    @classmethod
    def from_env(cls) -> "Config":
        env = os.environ
        channel = env.get("STATEMENT_UPDATER_CHANNEL", "")
        if not channel:
            raise SystemExit("STATEMENT_UPDATER_CHANNEL is not set. Run scripts/install-updater.sh first.")
        return cls(
            container=env.get("STATEMENT_CONTAINER", "statement-software-v5"),
            channel=Path(channel),
            work=Path(env.get("STATEMENT_UPDATER_WORK", "/opt/statement-updater/work")),
            repository=env.get("STATEMENT_REPOSITORY", "abdullah2444/statement-software-public"),
            api_base=env.get("STATEMENT_API_BASE", "https://api.github.com").rstrip("/"),
            build_args=shlex.split(env.get("STATEMENT_BUILD_ARGS", "")),
            extra_compose_files=[f for f in env.get("STATEMENT_EXTRA_COMPOSE_FILES", "").split(",") if f],
            health_timeout=int(env.get("STATEMENT_HEALTH_TIMEOUT", "180")),
            pause_timeout=int(env.get("STATEMENT_PAUSE_TIMEOUT", "120")),
        )


class Host:
    """Everything that touches Docker or the network. Tests replace this."""

    def run(self, args: list[str], timeout: int = 600, check: bool = True) -> subprocess.CompletedProcess:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        if check and proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip().splitlines()
            raise UpdateError(f"Command failed: {' '.join(args[:3])}: {detail[-1][:300] if detail else 'no output'}")
        return proc

    def http_json(self, url: str) -> dict | list:
        request = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json", "User-Agent": "statement-updater"})
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))

    def download(self, url: str, target: Path) -> None:
        request = urllib.request.Request(url, headers={"User-Agent": "statement-updater"})
        with urllib.request.urlopen(request, timeout=120) as response, target.open("wb") as out:
            total = 0
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_DOWNLOAD_BYTES:
                    raise UpdateError("The download is far bigger than a release should be.")
                out.write(chunk)

    def free_bytes(self, path: Path) -> int:
        while not path.exists():
            path = path.parent
        return shutil.disk_usage(path).free


@dataclass
class Container:
    """What we need to know about the running app container."""

    name: str
    image_id: str
    image_name: str
    env: dict
    mounts: list[dict]
    project: str
    working_dir: str
    config_files: list[str]
    service: str

    def host_path(self, container_path: str) -> Path | None:
        """Where a path inside the container lives on the server, if it is on a mounted volume."""
        best = None
        for mount in self.mounts:
            dest = mount.get("Destination", "").rstrip("/")
            if container_path == dest or container_path.startswith(dest + "/"):
                if best is None or len(dest) > len(best[0]):
                    best = (dest, mount.get("Source", ""))
        if best is None or not best[1]:
            return None
        return Path(best[1] + container_path[len(best[0]):])


class Updater:
    def __init__(self, config: Config, host: Host | None = None):
        self.config = config
        self.host = host or Host()
        self.channel = config.channel
        self.work = config.work
        self.log_file: Path | None = None
        self.run_record: dict = {}
        self._compose_cmd: list[str] | None = None

    # ── Bookkeeping ────────────────────────────────────────────────────────
    def log(self, message: str) -> None:
        line = f"{utc_timestamp()} {message}"
        print(line, flush=True)
        if self.log_file:
            with self.log_file.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")

    def set_stage(self, stage: str, message: str, state: str = "running") -> None:
        self.run_record.update({"stage": stage, "state": state, "message": message, "updated_at": utc_timestamp()})
        write_json(self.channel / "status.json", self.run_record)
        self.log(f"[{stage}] {message}")

    def finish(self, result: str, message: str) -> None:
        state = "done" if result == "installed" else "failed"
        stage = "complete" if result == "installed" else self.run_record.get("stage", "preparing")
        self.run_record.update({"result": result, "finished_at": utc_timestamp()})
        self.set_stage(stage, message, state)
        history = read_json(self.channel / "history.json", [])
        if not isinstance(history, list):
            history = []
        entry = {key: self.run_record.get(key) for key in (
            "id", "target", "from_version", "requested_by", "started_at", "finished_at", "result", "message", "backup",
        )}
        write_json(self.channel / "history.json", [entry, *history][:HISTORY_LIMIT])

    def heartbeat(self) -> None:
        write_json(self.channel / "heartbeat.json", {
            "at": utc_timestamp(), "pid": os.getpid(), "updater_version": UPDATER_VERSION,
        })

    def pause(self) -> None:
        (self.channel / "pause-ack.json").unlink(missing_ok=True)
        write_json(self.channel / "pause.json", {"at": utc_timestamp(), "id": self.run_record.get("id")})

    def unpause(self) -> None:
        (self.channel / "pause.json").unlink(missing_ok=True)

    # ── Docker ─────────────────────────────────────────────────────────────
    def inspect(self) -> Container:
        proc = self.host.run(["docker", "inspect", self.config.container], timeout=60)
        data = json.loads(proc.stdout)[0]
        labels = data["Config"].get("Labels") or {}
        env = dict(item.split("=", 1) for item in data["Config"].get("Env") or [] if "=" in item)
        if "com.docker.compose.project" not in labels:
            raise UpdateError(f"The app container {self.config.container} was not started with Docker Compose.")
        return Container(
            name=self.config.container,
            image_id=data["Image"],
            image_name=data["Config"]["Image"],
            env=env,
            mounts=data.get("Mounts") or [],
            project=labels["com.docker.compose.project"],
            working_dir=labels.get("com.docker.compose.project.working_dir", ""),
            config_files=[f for f in labels.get("com.docker.compose.project.config_files", "").split(",") if f],
            service=labels["com.docker.compose.service"],
        )

    def compose_cmd(self) -> list[str]:
        """Docker Compose v2 (`docker compose`), or the older `docker-compose`."""
        if self._compose_cmd is None:
            probe = self.host.run(["docker", "compose", "version"], check=False, timeout=30)
            self._compose_cmd = ["docker", "compose"] if probe.returncode == 0 else ["docker-compose"]
        return self._compose_cmd

    def compose(self, container: Container, *args: str, timeout: int = 600) -> subprocess.CompletedProcess:
        cmd = [*self.compose_cmd(), "-p", container.project]
        if container.working_dir:
            cmd += ["--project-directory", container.working_dir]
        # Extra files (the updater's own mount) are added once; afterwards the container's labels list them too.
        for config_file in dict.fromkeys([*container.config_files, *self.config.extra_compose_files]):
            cmd += ["-f", config_file]
        return self.host.run([*cmd, *args], timeout=timeout)

    def exec_in(self, container: str, args: list[str], timeout: int = 120, check: bool = True) -> subprocess.CompletedProcess:
        return self.host.run(["docker", "exec", container, *args], timeout=timeout, check=check)

    def helper(self, command: str, argument: str, timeout: int = 1800) -> dict:
        proc = self.exec_in(self.config.container, ["python", "update_helper.py", command, argument], timeout=timeout, check=False)
        lines = [line for line in (proc.stdout or "").splitlines() if line.strip().startswith("{")]
        try:
            result = json.loads(lines[-1]) if lines else {}
        except ValueError:
            result = {}
        if proc.returncode != 0 or not result.get("ok"):
            detail = result.get("error") or (proc.stderr or "").strip().splitlines()[-1:] or ["no output"]
            raise UpdateError(detail if isinstance(detail, str) else detail[0])
        return result

    def has_helper(self) -> bool:
        proc = self.exec_in(self.config.container, ["test", "-f", "update_helper.py"], timeout=30, check=False)
        return proc.returncode == 0

    def running_version(self) -> str:
        proc = self.exec_in(self.config.container, ["cat", "VERSION"], timeout=30, check=False)
        return proc.stdout.strip() if proc.returncode == 0 else ""

    def probe_health(self) -> dict:
        proc = self.exec_in(self.config.container, ["python", "-c", HEALTH_PROBE], timeout=30, check=False)
        lines = (proc.stdout or "").strip().splitlines()
        if proc.returncode != 0 or not lines:
            raise UpdateError("The app is not answering yet.")
        return json.loads(lines[-1])

    def wait_healthy(self, expected_version: str) -> None:
        deadline = time.monotonic() + self.config.health_timeout
        last_error = "The app did not start."
        while time.monotonic() < deadline:
            try:
                state = self.host.run(["docker", "inspect", "-f", "{{.State.Status}} {{.RestartCount}}", self.config.container], timeout=30)
                status, _, restarts = state.stdout.strip().partition(" ")
                if restarts.isdigit() and int(restarts) >= CRASH_RESTARTS:
                    # It keeps crashing on start; waiting longer won't help.
                    raise AppCrashing(f"The app keeps stopping right after it starts ({restarts} restarts).")
                if status != "running":
                    raise UpdateError(f"The app container is {status or 'missing'}.")
                payload = self.probe_health()
                if not payload.get("ok"):
                    raise UpdateError("The app says its database is not working.")
                if expected_version:
                    self.helper("health", expected_version, timeout=120)
                return
            except AppCrashing:
                raise
            except (UpdateError, ValueError) as exc:
                last_error = str(exc)
                time.sleep(3)
        raise UpdateError(f"The app did not pass its checks within {self.config.health_timeout} seconds: {last_error}")

    def compose_env_matches(self, container: Container) -> None:
        """Refuse to restart the app if Docker Compose would start it with different settings.

        For example, a SECRET_KEY that was set by hand when the app was started
        but isn't in the .env file would silently change after a restart.
        Only setting names are reported, never values.
        """
        if self.compose_cmd() != ["docker", "compose"]:
            self.log("Old docker-compose found; skipping the settings comparison.")
            return
        proc = self.compose(container, "config", "--format", "json", timeout=60)
        try:
            service = json.loads(proc.stdout)["services"][container.service]
        except (ValueError, KeyError) as exc:
            raise UpdateError("Could not read the Docker Compose settings.") from exc
        wanted = service.get("environment") or {}
        if isinstance(wanted, list):
            wanted = dict(item.split("=", 1) for item in wanted if "=" in item)
        different = sorted(k for k, v in wanted.items() if container.env.get(k, "") != ("" if v is None else str(v)))
        if different:
            raise UpdateError(
                "Restarting would change these app settings: " + ", ".join(different)
                + ". Put the values the app runs with now into the .env file next to the compose file, then try again."
            )

    # ── Release ────────────────────────────────────────────────────────────
    def fetch_release(self, tag: str) -> dict:
        url = f"{self.config.api_base}/repos/{self.config.repository}/releases/tags/{tag}"
        try:
            release = self.host.http_json(url)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise UpdateError(f"Release {tag} is not published on GitHub.") from exc
            raise UpdateError(f"GitHub answered with an error ({exc.code}).") from exc
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise UpdateError("Could not reach GitHub to look up the release.") from exc
        if not isinstance(release, dict) or release.get("tag_name") != tag:
            raise UpdateError("GitHub sent an answer this updater doesn't understand.")
        if release.get("draft") or release.get("prerelease"):
            raise UpdateError(f"Release {tag} is a draft or a test release, so it won't be installed.")
        if not release.get("tarball_url"):
            raise UpdateError(f"Release {tag} has no download.")
        return release

    def download_release(self, release: dict) -> Path:
        tag = release["tag_name"]
        downloads = self.work / "downloads"
        downloads.mkdir(parents=True, exist_ok=True)
        archive = downloads / f"{tag}.tar.gz"
        partial = archive.with_suffix(".part")
        try:
            self.host.download(release["tarball_url"], partial)
        except UpdateError:
            partial.unlink(missing_ok=True)
            raise
        except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
            partial.unlink(missing_ok=True)
            raise UpdateError(f"Downloading {tag} failed: {getattr(exc, 'reason', None) or exc.__class__.__name__}.") from exc
        os.replace(partial, archive)
        target = self.work / "releases" / tag
        if target.exists():
            shutil.rmtree(target)
        target.mkdir(parents=True)
        try:
            extract_release(archive, target)
        except Exception:
            shutil.rmtree(target, ignore_errors=True)
            raise
        for name in REQUIRED_RELEASE_FILES:
            if not (target / name).is_file():
                raise UpdateError(f"The download of {tag} is incomplete ({name} is missing).")
        version = (target / "VERSION").read_text(encoding="utf-8").strip()
        if "v" + version != tag:
            raise UpdateError(f"The download of {tag} says it is version {version}.")
        return target

    def check_disk_space(self, container: Container, release_dir: Path | None = None) -> None:
        db = container.host_path(container.env.get("DATABASE_PATH", "/data/firefly_statement.db"))
        uploads = container.host_path(container.env.get("UPLOAD_DIR", "/data/uploads"))
        backups = container.host_path(container.env.get("BACKUP_DIR", "/data/backups"))
        db_size = db.stat().st_size if db and db.exists() else 0
        uploads_size = sum(p.stat().st_size for p in uploads.rglob("*") if p.is_file()) if uploads and uploads.exists() else 0
        # A compressed backup is never bigger than its contents; keep room for a scratch copy too.
        needed = db_size * 3 + uploads_size + 300 * 1024 * 1024
        free = self.host.free_bytes(backups or self.channel)
        if free < needed:
            raise UpdateError(f"Not enough free disk space: {free // (1024 * 1024)} MB free, about {needed // (1024 * 1024)} MB needed.")
        work_free = self.host.free_bytes(self.work)
        if work_free < 1024 * 1024 * 1024:
            raise UpdateError(f"Not enough free disk space for the new version: {work_free // (1024 * 1024)} MB free, 1024 MB needed.")

    # ── Recovery backup ────────────────────────────────────────────────────
    def wait_for_pause_ack(self) -> None:
        deadline = time.monotonic() + self.config.pause_timeout
        while time.monotonic() < deadline:
            if (self.channel / "pause-ack.json").exists():
                return
            time.sleep(1)
        raise UpdateError("The app did not pause saving in time. Someone may be in the middle of a long save; try again in a few minutes.")

    def recovery_backup(self, container: Container, version: str) -> Path:
        result = self.helper("backup", f"before-update-to-{version}")
        backup = container.host_path(result["path"])
        if backup is None or not backup.exists():
            # The backup folder isn't mounted on the server: copy the file out of the container.
            local = self.work / "backups" / result["name"]
            local.parent.mkdir(parents=True, exist_ok=True)
            self.host.run(["docker", "cp", f"{container.name}:{result['path']}", str(local)], timeout=600)
            backup = local
        if sha256(backup) != result["sha256"]:
            raise UpdateError("The recovery backup changed after it was made.")
        self.log(f"Recovery backup {backup} ({result['size']} bytes, {result['upload_file_count']} receipts)"
                 + (f", cloud backup {result['cloud_snapshot']}" if result.get("cloud_snapshot") else ""))
        return backup

    def cold_backup(self, container: Container, version: str) -> Path:
        """For a first install over a version without the updater: stop the app and copy its files."""
        db = container.host_path(container.env.get("DATABASE_PATH", "/data/firefly_statement.db"))
        uploads = container.host_path(container.env.get("UPLOAD_DIR", "/data/uploads"))
        backups = container.host_path(container.env.get("BACKUP_DIR", "/data/backups")) or self.work / "backups"
        if db is None or not db.exists():
            raise UpdateError("Could not find the database file on the server.")
        self.host.run(["docker", "stop", "-t", "20", container.name], timeout=120)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        target = backups / f"statement-full-backup-{stamp}-before-update-to-{version}.tar.gz"
        make_cold_backup(db, uploads, target)
        self.log(f"Recovery backup {target} (made with the app stopped)")
        return target

    # ── Install and roll back ──────────────────────────────────────────────
    def compose_image_name(self, container: Container) -> str:
        """The image name Docker Compose starts the app from (the updater's compose file may rename it)."""
        try:
            proc = self.compose(container, "config", "--format", "json", timeout=60)
            name = json.loads(proc.stdout)["services"][container.service].get("image")
        except (UpdateError, ValueError, KeyError, TypeError):
            name = None
        return name or container.image_name

    def start_image(self, container: Container, image_id: str) -> None:
        self.host.run(["docker", "tag", image_id, self.compose_image_name(container)], timeout=60)
        args = ["up", "-d", "--no-build", "--force-recreate", "--no-deps"]
        if self.compose_cmd() == ["docker", "compose"]:
            args += ["--pull", "never"]
        self.compose(container, *args, container.service, timeout=600)

    def restore_data(self, container: Container, backup: Path) -> None:
        """Put the database from the recovery backup back, and any receipts that went missing."""
        db = container.host_path(container.env.get("DATABASE_PATH", "/data/firefly_statement.db"))
        uploads = container.host_path(container.env.get("UPLOAD_DIR", "/data/uploads"))
        if db is None:
            raise UpdateError("Could not find the database file on the server.")
        self.work.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=self.work) as tmp:
            extracted = Path(tmp)
            extract_backup(backup, extracted)
            restored = extracted / "firefly_statement.db"
            check_database(restored)
            staged = db.with_name(db.name + ".restoring")
            shutil.copy2(restored, staged)
            for suffix in ("-wal", "-shm"):
                db.with_name(db.name + suffix).unlink(missing_ok=True)
            os.replace(staged, db)
            if uploads is not None and (extracted / "uploads").is_dir():
                for item in (extracted / "uploads").rglob("*"):
                    if item.is_file():
                        dest = uploads / item.relative_to(extracted / "uploads")
                        if not dest.exists():
                            dest.parent.mkdir(parents=True, exist_ok=True)
                            shutil.copy2(item, dest)

    def rollback(self, container: Container, old_image_id: str, backup: Path, old_version: str,
                 why: str = "The new version didn't pass its checks.") -> None:
        self.set_stage("rolling_back", f"{why} Putting the previous version back…")
        self.host.run(["docker", "stop", "-t", "30", container.name], timeout=120, check=False)
        self.restore_data(container, backup)
        self.log("Database restored from the recovery backup")
        self.start_image(container, old_image_id)
        self.wait_healthy("")
        self.log(f"Previous version {old_version or 'unknown'} is running again")

    # ── One update ─────────────────────────────────────────────────────────
    def process(self, request: dict, allow_downgrade: bool = False) -> str:
        """Install one release. Returns the result: installed, not_started, rolled_back or rollback_failed.

        allow_downgrade is only ever set from the command line, never from a request file.
        """
        run_id = re.sub(r"[^A-Za-z0-9-]", "", str(request.get("id") or "")) or uuid.uuid4().hex[:12]
        tag = str(request.get("target") or "")
        logs = self.channel / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        self.log_file = logs / f"{run_id}.log"
        self.run_record = {
            "id": run_id,
            "target": tag,
            "from_version": "",
            "requested_by": str(request.get("requested_by") or "")[:100],
            "started_at": utc_timestamp(),
            "result": "",
        }
        container = None
        backup: Path | None = None
        old_image_id = ""
        installing = False
        try:
            self.set_stage("preparing", f"Getting version {tag.lstrip('v')} ready…")
            target = parse_tag(tag)
            container = self.inspect()
            old_image_id = container.image_id
            from_version = self.running_version()
            self.run_record["from_version"] = from_version
            if target <= parse_version(from_version) and not allow_downgrade:
                raise UpdateError(f"Version {from_version} is already installed, which is the same as or newer than {tag}.")
            self.run_record.update({"old_image": old_image_id})
            self.compose_env_matches(container)
            release = self.fetch_release(tag)
            self.check_disk_space(container)
            self.set_stage("preparing", f"Downloading version {tag.lstrip('v')}…")
            release_dir = self.download_release(release)
            self.set_stage("preparing", f"Building version {tag.lstrip('v')}. The app keeps running meanwhile…")
            image = f"{RELEASE_IMAGE}:{tag.lstrip('v')}"
            self.host.run(["docker", "build", *self.config.build_args, "-t", image, str(release_dir)], timeout=3600)
            new_image_id = self.host.run(["docker", "image", "inspect", "-f", "{{.Id}}", image], timeout=60).stdout.strip()
            # Keep the old image under its own name, so it can't be cleaned up while we may need it.
            self.host.run(["docker", "tag", old_image_id, f"{RELEASE_IMAGE}:previous"], timeout=60)

            self.set_stage("backing_up", "Pausing saving and making a recovery backup…")
            if self.has_helper():
                self.pause()
                self.wait_for_pause_ack()
                backup = self.recovery_backup(container, tag.lstrip("v"))
                self.exec_in(container.name, ["python", "update_helper.py", "checkpoint", "-"], timeout=120, check=False)
            else:
                self.pause()
                backup = self.cold_backup(container, tag.lstrip("v"))
            self.run_record["backup"] = backup.name
            self.run_record["backup_path"] = str(backup)

            installing = True
            self.set_stage("installing", f"Restarting the app on version {tag.lstrip('v')}…")
            self.start_image(container, new_image_id)

            self.set_stage("checking", "Checking that the new version and your data work…")
            self.wait_healthy(tag)
            self.unpause()
            self.cleanup_old_releases(tag)
            self.finish("installed", f"Updated from {from_version or 'the old version'} to {tag.lstrip('v')}. Saving is back on.")
            return "installed"
        except Exception as exc:
            reason = str(exc) if isinstance(exc, UpdateError) else f"Unexpected error: {exc}"
            reason = reason if reason.endswith(".") else reason + "."
            self.log(traceback.format_exc())
            if not installing:
                if container is not None and not self.has_running(container):
                    # A backup made with the app stopped was interrupted; start the old app again.
                    try:
                        self.start_image(container, old_image_id)
                        self.wait_healthy("")
                    except Exception as start_exc:
                        self.log(f"Could not restart the previous version: {start_exc}")
                        self.finish("rollback_failed", f"{reason} The app could not be started again: {start_exc}")
                        return "rollback_failed"
                self.unpause()
                self.finish("not_started", f"{reason} Nothing was changed.")
                return "not_started"
            try:
                self.rollback(container, old_image_id, backup, self.run_record.get("from_version", ""))
            except Exception as rb_exc:
                self.log(traceback.format_exc())
                self.finish(
                    "rollback_failed",
                    f"{reason} Putting the previous version back also failed ({rb_exc}). Saving stays paused. "
                    f"The recovery backup is {backup.name if backup else 'missing'}. Get help before changing anything.",
                )
                return "rollback_failed"
            self.unpause()
            self.finish("rolled_back", f"{reason} The previous version and your data were put back. Saving is back on.")
            return "rolled_back"
        finally:
            self.log_file = None

    def has_running(self, container: Container) -> bool:
        proc = self.host.run(["docker", "inspect", "-f", "{{.State.Status}}", container.name], timeout=30, check=False)
        return proc.stdout.strip().split(" ")[0] == "running"

    def cleanup_old_releases(self, keep_tag: str) -> None:
        releases = self.work / "releases"
        if not releases.exists():
            return
        dirs = sorted((d for d in releases.iterdir() if d.is_dir()), key=lambda d: d.stat().st_mtime, reverse=True)
        for old in dirs[KEEP_RELEASES:]:
            if old.name != keep_tag:
                shutil.rmtree(old, ignore_errors=True)
        downloads = self.work / "downloads"
        for archive in downloads.glob("*.tar.gz") if downloads.exists() else []:
            if archive.name != f"{keep_tag}.tar.gz":
                archive.unlink(missing_ok=True)

    def recover_interrupted(self) -> None:
        """The updater stopped in the middle of an update (power cut, reboot). Leave things safe."""
        status = read_json(self.channel / "status.json", {})
        if status.get("state") != "running":
            return
        self.run_record = status
        (self.channel / "logs").mkdir(parents=True, exist_ok=True)
        self.log_file = self.channel / "logs" / f"{re.sub(r'[^A-Za-z0-9-]', '', str(status.get('id'))) or 'interrupted'}.log"
        stage = status.get("stage")
        self.log(f"Found an update that was interrupted while {stage}")
        try:
            if stage in ("installing", "checking", "rolling_back") and status.get("backup_path") and status.get("old_image"):
                container = self.inspect()
                self.rollback(container, status["old_image"], Path(status["backup_path"]), status.get("from_version", ""),
                              why="The update was interrupted.")
                self.unpause()
                self.finish("rolled_back", "The update was interrupted. The previous version and your data were put back.")
            else:
                container = self.inspect()
                if not self.has_running(container):
                    self.start_image(container, status.get("old_image") or container.image_id)
                    self.wait_healthy("")
                self.unpause()
                self.finish("not_started", "The update was interrupted before anything was changed.")
        except Exception as exc:
            self.log(traceback.format_exc())
            self.finish("rollback_failed", f"The update was interrupted and the app could not be put back ({exc}). Saving stays paused.")
        finally:
            self.log_file = None

    def process_locked(self, request: dict, allow_downgrade: bool = False) -> str:
        """Run one update, making sure no other updater process runs one at the same time."""
        self.work.mkdir(parents=True, exist_ok=True)
        with (self.work / "update.lock").open("w") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise SystemExit("Another update is running right now.")
            return self.process(request, allow_downgrade)

    # ── Service loop ───────────────────────────────────────────────────────
    def take_request(self) -> dict | None:
        path = self.channel / "request.json"
        if not path.exists():
            return None
        claimed = self.channel / f".request-{uuid.uuid4().hex}.json"
        try:
            os.replace(path, claimed)
        except FileNotFoundError:
            return None
        request = read_json(claimed, {})
        claimed.unlink(missing_ok=True)
        return request if isinstance(request, dict) else {}

    def serve(self) -> None:
        self.channel.mkdir(parents=True, exist_ok=True)
        self.work.mkdir(parents=True, exist_ok=True)
        stop = threading.Event()

        def beat():
            while not stop.is_set():
                try:
                    self.heartbeat()
                except OSError as exc:
                    print(f"heartbeat failed: {exc}", flush=True)
                stop.wait(5)

        threading.Thread(target=beat, name="heartbeat", daemon=True).start()
        self.recover_interrupted()
        self.log(f"Updater ready. Watching {self.channel} for update requests.")
        try:
            while True:
                request = self.take_request()
                if request is not None:
                    try:
                        self.process_locked(request)
                    except SystemExit as exc:
                        self.log(f"Update request ignored: {exc}")
                time.sleep(self.config.poll_seconds)
        finally:
            stop.set()


# ── File helpers (shared with tests) ─────────────────────────────────────────
def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def extract_release(archive: Path, target: Path) -> None:
    """Unpack a GitHub release tarball (which has one top folder) into target, refusing anything unsafe."""
    try:
        with tarfile.open(archive, "r:gz") as tar:
            members = tar.getmembers()
            total = 0
            for member in members:
                parts = Path(member.name).parts
                if member.name.startswith("/") or ".." in parts:
                    raise UpdateError(f"The download contains an unsafe path: {member.name}")
                if not (member.isfile() or member.isdir()):
                    if member.type == tarfile.XGLTYPE or member.type == tarfile.XHDTYPE:
                        continue
                    raise UpdateError(f"The download contains a link or special file: {member.name}")
                total += member.size
                if total > MAX_EXTRACTED_BYTES:
                    raise UpdateError("The download unpacks to far more than a release should.")
            for member in members:
                parts = Path(member.name).parts
                if len(parts) < 2 or not (member.isfile() or member.isdir()):
                    continue
                dest = target.joinpath(*parts[1:])
                if member.isdir():
                    dest.mkdir(parents=True, exist_ok=True)
                    continue
                dest.parent.mkdir(parents=True, exist_ok=True)
                with tar.extractfile(member) as src, dest.open("wb") as out:
                    shutil.copyfileobj(src, out)
                dest.chmod(0o755 if member.mode & 0o111 else 0o644)
    except (tarfile.TarError, EOFError, OSError) as exc:
        raise UpdateError(f"The download is damaged: {exc}") from exc


def check_database(path: Path) -> None:
    db = sqlite3.connect(str(path))
    try:
        row = db.execute("pragma integrity_check").fetchone()
        if not row or str(row[0]).lower() != "ok":
            raise UpdateError("The database in the recovery backup failed its integrity check.")
        tables = {r[0] for r in db.execute("select name from sqlite_master where type = 'table'")}
    except sqlite3.DatabaseError as exc:
        raise UpdateError("The recovery backup does not contain a valid database.") from exc
    finally:
        db.close()
    if not {"users", "app_settings"} <= tables:
        raise UpdateError("The database in the recovery backup is missing tables.")


def extract_backup(archive: Path, target: Path) -> None:
    with tarfile.open(archive, "r:gz") as tar:
        for member in tar.getmembers():
            if member.issym() or member.islnk() or member.name.startswith("/") or ".." in Path(member.name).parts:
                raise UpdateError(f"The recovery backup contains an unsafe entry: {member.name}")
        # Members were checked above; the "data" filter adds the standard library's own checks where available.
        tar.extractall(target, **({"filter": "data"} if hasattr(tarfile, "data_filter") else {}))


def make_cold_backup(db: Path, uploads: Path | None, target: Path) -> None:
    """Write a full backup in the same format the app uses, then check it."""
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=target.parent) as tmp:
        staging = Path(tmp)
        source = sqlite3.connect(str(db))
        copy = sqlite3.connect(str(staging / "firefly_statement.db"))
        try:
            source.backup(copy)
        finally:
            copy.close()
            source.close()
        check_database(staging / "firefly_statement.db")
        upload_count = 0
        (staging / "uploads").mkdir()
        if uploads is not None and uploads.exists():
            for item in uploads.rglob("*"):
                if item.is_file() and not item.is_symlink():
                    dest = staging / "uploads" / item.relative_to(uploads)
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(item, dest)
                    upload_count += 1
        manifest = {
            "format_version": 1,
            "app_name": "Statement Software v5",
            "created_at_utc": utc_timestamp(),
            "database_file": "firefly_statement.db",
            "uploads_dir": "uploads",
            "upload_file_count": upload_count,
            "notes": "Recovery backup made by the updater with the app stopped.",
        }
        (staging / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        partial = target.with_name(target.name + ".part")
        with tarfile.open(partial, "w:gz") as tar:
            for name in ("manifest.json", "firefly_statement.db", "uploads"):
                tar.add(staging / name, arcname=name)
        os.replace(partial, target)
    with tempfile.TemporaryDirectory(dir=target.parent) as tmp:
        extract_backup(target, Path(tmp))
        check_database(Path(tmp) / "firefly_statement.db")
        found = sum(1 for p in (Path(tmp) / "uploads").rglob("*") if p.is_file())
        if found != upload_count:
            raise UpdateError("The recovery backup is missing receipts.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Statement Software host updater")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run", help="run as a service")
    install = sub.add_parser("install", help="install one release now")
    install.add_argument("tag")
    install.add_argument("--allow-downgrade", action="store_true")
    sub.add_parser("status", help="print the last status")
    args = parser.parse_args(argv)
    config = Config.from_env()
    updater = Updater(config)
    if args.command == "run":
        updater.serve()
        return 0
    if args.command == "status":
        print(json.dumps(read_json(config.channel / "status.json", {}), indent=2))
        return 0
    config.channel.mkdir(parents=True, exist_ok=True)
    config.work.mkdir(parents=True, exist_ok=True)
    updater.heartbeat()
    result = updater.process_locked({
        "id": datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-cli",
        "target": args.tag,
        "requested_by": "command line",
    }, allow_downgrade=args.allow_downgrade)
    print(json.dumps(read_json(config.channel / "status.json", {}), indent=2))
    return 0 if result == "installed" else 1


if __name__ == "__main__":
    sys.exit(main())
