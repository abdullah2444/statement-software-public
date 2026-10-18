"""Encrypted offsite backups to Backblaze B2, using restic.

restic (https://restic.net) does the encryption, deduplication, integrity
checks and retention. This module only builds the restic command line, runs
it with bounded timeouts, and turns its JSON output into plain Python values.
It knows nothing about Flask; app.py owns settings, scheduling and the UI.

Secrets (the B2 application key and the restic password, which the UI calls
the "recovery key") are passed to restic through environment variables and
are never logged or included in error messages.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

RESTIC_HOST = "statement-software"
RESTIC_TAG = "statement-software"
BACKUP_TIMEOUT_SECONDS = 30 * 60
DEFAULT_TIMEOUT_SECONDS = 5 * 60

_REGION_RE = re.compile(r"^[a-z]{2}-[a-z]+-\d{3}$")
_ENDPOINT_RE = re.compile(r"^(?:https://)?s3\.([a-z]{2}-[a-z]+-\d{3})\.backblazeb2\.com/?$")
_BUCKET_RE = re.compile(r"^[A-Za-z0-9-]{6,63}$")
_PREFIX_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$")
_KEY_ID_RE = re.compile(r"^[A-Za-z0-9]{10,64}$")
_APP_KEY_RE = re.compile(r"^[A-Za-z0-9+/=_-]{10,128}$")
_SNAPSHOT_ID_RE = re.compile(r"^[0-9a-f]{8,64}$")
_MISSING_REPO_MARKERS = (
    "is there a repository at the following location",
    "repository does not exist",
    "unable to open config file",
)
_WRONG_PASSWORD_MARKERS = ("wrong password", "no key found")


class CloudBackupError(Exception):
    """A user-facing failure. The message never contains secrets."""


class RepositoryMissing(CloudBackupError):
    pass


class WrongRecoveryKey(CloudBackupError):
    pass


@dataclass(frozen=True)
class B2Config:
    region: str
    bucket: str
    prefix: str
    key_id: str
    app_key: str
    recovery_key: str

    @property
    def repository(self) -> str:
        return f"s3:https://s3.{self.region}.backblazeb2.com/{self.bucket}/{self.prefix}"

    @property
    def location_label(self) -> str:
        return f"{self.bucket}/{self.prefix}"


def parse_region(value: str) -> str:
    """Accept either a region (us-west-004) or the S3 endpoint B2 shows for a bucket."""
    value = (value or "").strip().lower()
    match = _ENDPOINT_RE.match(value)
    if match:
        return match.group(1)
    if _REGION_RE.match(value):
        return value
    raise CloudBackupError(
        "Endpoint must look like s3.us-west-004.backblazeb2.com (copy it from your bucket's page in Backblaze)."
    )


def validate_config(region: str, bucket: str, prefix: str, key_id: str, app_key: str, recovery_key: str) -> B2Config:
    region = parse_region(region)
    if not _BUCKET_RE.match(bucket or ""):
        raise CloudBackupError("Bucket name must be 6-63 letters, numbers or dashes.")
    if not _PREFIX_RE.match(prefix or ""):
        raise CloudBackupError("Folder name must start with a letter or number and use only letters, numbers, dots, dashes or underscores.")
    if not _KEY_ID_RE.match(key_id or ""):
        raise CloudBackupError("Key ID doesn't look right. Copy the keyID shown when you created the application key.")
    if not _APP_KEY_RE.match(app_key or ""):
        raise CloudBackupError("Application key doesn't look right. Copy the applicationKey shown when you created it.")
    if len(recovery_key or "") < 16:
        raise CloudBackupError("Recovery key must be at least 16 characters.")
    return B2Config(region, bucket, prefix, key_id, app_key, recovery_key)


def restic_available() -> bool:
    return shutil.which(restic_binary()) is not None


def restic_binary() -> str:
    return os.environ.get("RESTIC_BINARY", "restic")


class Restic:
    """Thin wrapper around the restic CLI for one repository."""

    def __init__(self, config: B2Config, cache_dir: Path | None = None, repository: str | None = None):
        self.config = config
        self.cache_dir = cache_dir
        # `repository` overrides the B2 location; tests use a local directory.
        self.repository = repository or config.repository

    def _env(self) -> dict:
        env = {
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "HOME": os.environ.get("HOME", "/tmp"),
            "RESTIC_REPOSITORY": self.repository,
            "RESTIC_PASSWORD": self.config.recovery_key,
            "AWS_ACCESS_KEY_ID": self.config.key_id,
            "AWS_SECRET_ACCESS_KEY": self.config.app_key,
        }
        if self.cache_dir is not None:
            env["RESTIC_CACHE_DIR"] = str(self.cache_dir)
        return env

    def _scrub(self, text: str) -> str:
        for secret in (self.config.app_key, self.config.recovery_key, self.config.key_id):
            if secret:
                text = text.replace(secret, "[hidden]")
        return text

    def run(self, args: list[str], timeout: int = DEFAULT_TIMEOUT_SECONDS, cwd: Path | None = None) -> str:
        if not restic_available():
            raise CloudBackupError("The restic backup tool is not installed on this server.")
        try:
            proc = subprocess.run(
                [restic_binary(), *args],
                env=self._env(),
                cwd=str(cwd) if cwd else None,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise CloudBackupError(f"Backblaze did not respond within {timeout // 60} minutes.") from exc
        if proc.returncode != 0:
            message = self._scrub((proc.stderr or proc.stdout or "").strip())
            lower = message.lower()
            if any(marker in lower for marker in _MISSING_REPO_MARKERS):
                raise RepositoryMissing("No backups exist in this bucket folder yet.")
            if any(marker in lower for marker in _WRONG_PASSWORD_MARKERS):
                raise WrongRecoveryKey("The recovery key does not match the backups in this bucket folder.")
            if "403" in message or "access denied" in lower or "invalidaccesskeyid" in lower or "signaturedoesnotmatch" in lower:
                raise CloudBackupError("Backblaze refused the key. Check the key ID, application key and bucket name.")
            last_line = message.splitlines()[-1] if message else "unknown error"
            raise CloudBackupError(f"Backup tool error: {last_line[:300]}")
        return proc.stdout

    def _json_lines(self, output: str) -> list[dict]:
        rows = []
        for line in output.splitlines():
            line = line.strip()
            if line.startswith("{") or line.startswith("["):
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return rows

    def exists(self) -> bool:
        try:
            self.run(["cat", "config"])
        except RepositoryMissing:
            return False
        return True

    def init(self) -> None:
        self.run(["init"])

    def unlock(self) -> None:
        """Remove stale locks left by a backup that was interrupted (restic keeps live locks)."""
        self.run(["unlock"])

    def backup(self, source_dir: Path, tags: list[str] | None = None) -> dict:
        args = ["backup", "--json", "--host", RESTIC_HOST, "--tag", RESTIC_TAG]
        for tag in tags or []:
            args += ["--tag", tag]
        args.append(".")
        output = self.run(args, timeout=BACKUP_TIMEOUT_SECONDS, cwd=source_dir)
        summary = next((row for row in reversed(self._json_lines(output)) if row.get("message_type") == "summary"), None)
        if not summary or not summary.get("snapshot_id"):
            raise CloudBackupError("Backup finished without confirming a saved snapshot.")
        return summary

    def snapshots(self) -> list[dict]:
        output = self.run(["snapshots", "--json", "--host", RESTIC_HOST, "--tag", RESTIC_TAG])
        rows = json.loads(output or "[]") or []
        rows.sort(key=lambda row: row.get("time", ""), reverse=True)
        return rows

    def forget(self, keep_days: int, prune: bool) -> None:
        args = [
            "forget", "--host", RESTIC_HOST, "--tag", RESTIC_TAG, "--group-by", "host",
            "--keep-within", f"{int(keep_days)}d",
        ]
        if prune:
            args.append("--prune")
        self.run(args, timeout=BACKUP_TIMEOUT_SECONDS)

    def check(self, read_data_percent: int = 5) -> None:
        self.run(["check", f"--read-data-subset={int(read_data_percent)}%"], timeout=BACKUP_TIMEOUT_SECONDS)

    def restore(self, snapshot: dict, target_dir: Path) -> Path:
        """Restore a snapshot into target_dir and return the folder holding the backed-up files."""
        snapshot_id = snapshot.get("id", "")
        if not _SNAPSHOT_ID_RE.match(snapshot_id):
            raise CloudBackupError("Unknown backup.")
        target_dir.mkdir(parents=True, exist_ok=True)
        self.run(["restore", snapshot_id, "--target", str(target_dir)], timeout=BACKUP_TIMEOUT_SECONDS)
        # Backups are taken of "." so the files sit at the top. Older restic versions
        # may nest them under the absolute path that was backed up.
        candidates = [target_dir] + [target_dir / path.lstrip("/") for path in snapshot.get("paths") or []]
        base = target_dir.resolve()
        for candidate in candidates:
            resolved = candidate.resolve()
            if resolved.is_relative_to(base) and (resolved / "manifest.json").is_file():
                return resolved
        raise CloudBackupError("Restored backup is missing its files.")

    def stats_bytes(self) -> int | None:
        output = self.run(["stats", "--json", "--mode", "raw-data"])
        rows = self._json_lines(output)
        if rows and isinstance(rows[-1], dict):
            return rows[-1].get("total_size")
        return None
