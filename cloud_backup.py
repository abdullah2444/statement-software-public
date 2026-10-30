"""Encrypted offsite backups to Backblaze B2, and optionally a second copy in
Cloudflare R2, using restic.

restic (https://restic.net) does the encryption, deduplication, integrity
checks and retention. This module only builds the restic command line, runs
it with bounded timeouts, and turns its JSON output into plain Python values.
It knows nothing about Flask; app.py owns settings, scheduling and the UI.

Secrets (the B2 application key, the R2 secret access key, and the restic password, which the UI calls
the "recovery key") are passed to restic through environment variables and
are never logged or included in error messages.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import shutil
import subprocess
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import ClassVar
from urllib.parse import quote

import requests

B2_AUTHORIZE_URL = "https://api.backblazeb2.com/b2api/v4/b2_authorize_account"
B2_REQUIRED_CAPABILITIES = {"listFiles", "readFiles", "writeFiles", "deleteFiles"}
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
# restic retries a file Backblaze won't serve for up to 15 minutes, printing e.g.
# "Load(<snapshot/4c58ada185>, 0, 0) returned error, retrying after 610ms: ...".
# After this many tries of the same file it is stopped, with a clear message.
_RETRY_LINE_RE = re.compile(r"^\s*\w+\(<([^>]+)>.*\) returned error, retrying after [^:]*:\s*(.*)$")
RETRIES_BEFORE_GIVING_UP = 6
# Backblaze's daily free limits ("Caps & Alerts"), as its API and S3 errors word them:
# transaction_cap_exceeded, download_cap_exceeded, "... (Class B) cap exceeded".
_CAP_RE = re.compile(r"cap[_ ]exceeded", re.IGNORECASE)
CAP_REACHED_MESSAGE = (
    "Backblaze's daily free limit has been reached (Caps & Alerts in your Backblaze account), "
    "so it is refusing for now. Backups work again after Backblaze's daily reset, or sooner if "
    "you raise the cap there. Your data on this server is safe."
)


def _cap_reached(response) -> bool:
    try:
        code = str((response.json() or {}).get("code", ""))
    except (ValueError, AttributeError, TypeError):
        return False
    return bool(_CAP_RE.search(code))


class CloudBackupError(Exception):
    """A user-facing failure. The message never contains secrets."""


class FileKeepsFailing(CloudBackupError):
    def __init__(self, message: str, reason: str = ""):
        super().__init__(message)
        self.reason = reason  # restic's full retry message, for telling a daily-limit refusal apart


def run_watched(command: list[str], env: dict, cwd: str | None, timeout: int,
                service: str = "Backblaze", status_page: str = "status.backblaze.com") -> subprocess.CompletedProcess:
    """Run restic like subprocess.run, but stop it once the storage service keeps refusing one file.

    Raises subprocess.TimeoutExpired, or FileKeepsFailing naming the file.
    """
    proc = subprocess.Popen(command, env=env, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    out: list[str] = []
    err: list[str] = []
    tries: dict[str, int] = {}
    stuck: list[tuple[str, str]] = []

    def read_stderr() -> None:
        for line in proc.stderr:
            err.append(line)
            match = _RETRY_LINE_RE.match(line)
            if match and not stuck:
                name = match.group(1)
                tries[name] = tries.get(name, 0) + 1
                if tries[name] >= RETRIES_BEFORE_GIVING_UP:
                    stuck.append((name, match.group(2).strip()))
                    proc.kill()

    readers = [
        threading.Thread(target=lambda: out.extend(proc.stdout), daemon=True),
        threading.Thread(target=read_stderr, daemon=True),
    ]
    for reader in readers:
        reader.start()
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        raise
    finally:
        for reader in readers:
            reader.join(timeout=5)
    if stuck:
        name, reason = stuck[0]
        raise FileKeepsFailing(
            f"{service} keeps refusing one backup file ({name}): {reason[:120]}. "
            f"This is a problem on {service}'s side; see {status_page}. "
            "Your data on this server is safe.",
            reason,
        )
    return subprocess.CompletedProcess(command, proc.returncode, "".join(out), "".join(err))


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
    service: ClassVar[str] = "Backblaze"
    status_page: ClassVar[str] = "status.backblaze.com"

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


def discover_b2(key_id: str, app_key: str) -> tuple[str, str | None]:
    """Ask Backblaze which region and bucket an application key belongs to.

    Returns (region, bucket). bucket is None when the key can reach every
    bucket in the account, so the user has to name one.
    """
    if not _KEY_ID_RE.match(key_id or "") or not _APP_KEY_RE.match(app_key or ""):
        raise CloudBackupError("Key ID or Application Key doesn't look right. Copy both from Backblaze again.")
    try:
        response = requests.get(B2_AUTHORIZE_URL, auth=(key_id, app_key), timeout=20)
    except requests.RequestException as exc:
        raise CloudBackupError("Could not reach Backblaze. Check this server's internet connection.") from exc
    if response.status_code != 200 and _cap_reached(response):
        raise CloudBackupError(CAP_REACHED_MESSAGE)
    if response.status_code == 401:
        raise CloudBackupError("Backblaze didn't accept this Key ID and Application Key. Copy both again.")
    if response.status_code != 200:
        raise CloudBackupError(f"Backblaze answered with an error ({response.status_code}). Try again in a minute.")
    try:
        storage = response.json()["apiInfo"]["storageApi"]
        region = parse_region(storage["s3ApiUrl"].replace("https://", ""))
    except (ValueError, KeyError, TypeError) as exc:
        raise CloudBackupError("Backblaze sent an answer this app doesn't understand.") from exc
    allowed = storage.get("allowed") or {}
    missing = B2_REQUIRED_CAPABILITIES - set(allowed.get("capabilities") or [])
    if missing:
        raise CloudBackupError("This key can't write backups. Create it again with Read and Write access.")
    buckets = [b.get("name") for b in allowed.get("buckets") or [] if b.get("name")]
    return region, (buckets[0] if len(buckets) == 1 else None)


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


R2_HOST_SUFFIX = ".r2.cloudflarestorage.com"
# What Cloudflare shows as the bucket's "S3 API" address, with or without the bucket:
# https://<32-character account id>[.eu].r2.cloudflarestorage.com[/<bucket>]
_R2_ENDPOINT_RE = re.compile(
    r"^(?:https?://)?([0-9a-f]{32}(?:\.[a-z]+)?)\.r2\.cloudflarestorage\.com(?:/([a-z0-9][a-z0-9-]{1,61}[a-z0-9]))?/?$"
)
_R2_BUCKET_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$")
_R2_KEY_ID_RE = re.compile(r"^[A-Za-z0-9]{16,128}$")
_R2_SECRET_RE = re.compile(r"^[A-Za-z0-9+/=_-]{16,128}$")


@dataclass(frozen=True)
class R2Config:
    """A second copy in Cloudflare R2. Field names match B2Config so Restic treats both alike:
    key_id is the Access Key ID and app_key the Secret Access Key."""
    host: str
    bucket: str
    prefix: str
    key_id: str
    app_key: str
    recovery_key: str
    service: ClassVar[str] = "Cloudflare R2"
    status_page: ClassVar[str] = "www.cloudflarestatus.com"

    @property
    def repository(self) -> str:
        return f"s3:https://{self.host}/{self.bucket}/{self.prefix}"

    @property
    def location_label(self) -> str:
        return f"{self.bucket}/{self.prefix}"


def parse_r2_endpoint(value: str) -> tuple[str, str]:
    """(host, bucket) from the S3 API address on the bucket's page in Cloudflare. bucket may be ""."""
    match = _R2_ENDPOINT_RE.match((value or "").strip().lower())
    if not match:
        raise CloudBackupError(
            "Paste the S3 API address from your bucket's Settings page in Cloudflare. "
            "It looks like https://<account id>.r2.cloudflarestorage.com/<bucket>."
        )
    return match.group(1) + R2_HOST_SUFFIX, match.group(2) or ""


def validate_r2_config(endpoint: str, bucket: str, prefix: str, key_id: str, secret: str, recovery_key: str) -> R2Config:
    host, found_bucket = parse_r2_endpoint(endpoint)
    bucket = (bucket or found_bucket).strip().lower()
    if not _R2_BUCKET_RE.match(bucket):
        raise CloudBackupError("Bucket name must be 3-63 lowercase letters, numbers or dashes.")
    if not _PREFIX_RE.match(prefix or ""):
        raise CloudBackupError("Folder name must start with a letter or number and use only letters, numbers, dots, dashes or underscores.")
    if not _R2_KEY_ID_RE.match(key_id or ""):
        raise CloudBackupError("Access Key ID doesn't look right. Copy it from the R2 API token page again.")
    if not _R2_SECRET_RE.match(secret or ""):
        raise CloudBackupError("Secret Access Key doesn't look right. Copy it from the R2 API token page again.")
    if len(recovery_key or "") < 16:
        raise CloudBackupError("Set up the main cloud backup first: the second copy uses the same recovery key.")
    return R2Config(host, bucket, prefix, key_id, secret, recovery_key)


def sigv4_headers(host: str, path: str, query: dict, key_id: str, secret: str,
                  region: str = "auto", now: datetime | None = None) -> dict:
    """AWS Signature Version 4 headers for an empty-body GET (all the R2 key check needs)."""
    now = now or datetime.now(timezone.utc)
    amz_date, day = now.strftime("%Y%m%dT%H%M%SZ"), now.strftime("%Y%m%d")
    payload = hashlib.sha256(b"").hexdigest()
    canonical_query = "&".join(
        f"{quote(str(k), safe='-_.~')}={quote(str(v), safe='-_.~')}" for k, v in sorted(query.items())
    )
    signed = "host;x-amz-content-sha256;x-amz-date"
    request = "\n".join([
        "GET", quote(path, safe="/-_.~"), canonical_query,
        f"host:{host}\nx-amz-content-sha256:{payload}\nx-amz-date:{amz_date}\n", signed, payload,
    ])
    scope = f"{day}/{region}/s3/aws4_request"
    to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(request.encode()).hexdigest()])
    key = ("AWS4" + secret).encode()
    for part in (day, region, "s3", "aws4_request"):
        key = hmac.new(key, part.encode(), hashlib.sha256).digest()
    signature = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
    return {
        "x-amz-date": amz_date,
        "x-amz-content-sha256": payload,
        "Authorization": f"AWS4-HMAC-SHA256 Credential={key_id}/{scope}, SignedHeaders={signed}, Signature={signature}",
    }


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
        self._key_checked = self.repository != config.repository

    def check_key(self) -> None:
        """Ask Backblaze whether the key works and reaches the bucket.

        restic keeps retrying a refused key for many minutes, so without this a
        wrong key looks like Backblaze not answering (and an update stays paused).
        """
        if self._key_checked:
            return
        if isinstance(self.config, R2Config):
            self._check_r2_key()
            self._key_checked = True
            return
        try:
            response = requests.get(B2_AUTHORIZE_URL, auth=(self.config.key_id, self.config.app_key), timeout=20)
        except requests.RequestException as exc:
            raise CloudBackupError("Could not reach Backblaze. Check this server's internet connection.") from exc
        if response.status_code != 200 and _cap_reached(response):
            raise CloudBackupError(CAP_REACHED_MESSAGE)
        if response.status_code in (401, 403):
            raise CloudBackupError("Backblaze refused the key. Check the key ID and application key.")
        if response.status_code != 200:
            raise CloudBackupError(f"Backblaze answered with an error ({response.status_code}). Try again in a minute.")
        try:
            allowed = response.json()["apiInfo"]["storageApi"].get("allowed") or {}
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise CloudBackupError("Backblaze sent an answer this app doesn't understand.") from exc
        buckets = [b.get("name") for b in allowed.get("buckets") or [] if b.get("name")]
        if buckets and self.config.bucket not in buckets:
            raise CloudBackupError(f"This key can't reach the bucket {self.config.bucket}. Check the bucket name.")
        self._key_checked = True

    def _check_r2_key(self) -> None:
        """List one name in the bucket folder, so a wrong key or bucket is reported at once."""
        config = self.config
        query = {"list-type": "2", "max-keys": "1", "prefix": f"{config.prefix}/"}
        path = f"/{config.bucket}"
        headers = sigv4_headers(config.host, path, query, config.key_id, config.app_key)
        try:
            response = requests.get(f"https://{config.host}{path}", params=query, headers=headers, timeout=20)
        except requests.RequestException as exc:
            raise CloudBackupError(
                "Could not reach Cloudflare R2. Check the S3 API address and this server's internet connection."
            ) from exc
        if response.status_code == 200:
            return
        if response.status_code == 404 or "NoSuchBucket" in (response.text or ""):
            raise CloudBackupError(f"Cloudflare R2 has no bucket named {config.bucket} in this account. Check the bucket name.")
        if response.status_code in (401, 403):
            raise CloudBackupError(
                "Cloudflare R2 refused the key. Check the Access Key ID and Secret Access Key, "
                "and that the token has Object Read & Write for this bucket."
            )
        raise CloudBackupError(f"Cloudflare R2 answered with an error ({response.status_code}). Try again in a minute.")

    def _env(self) -> dict:
        env = {
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "HOME": os.environ.get("HOME", "/tmp"),
            "RESTIC_REPOSITORY": self.repository,
            "RESTIC_PASSWORD": self.config.recovery_key,
            "AWS_ACCESS_KEY_ID": self.config.key_id,
            "AWS_SECRET_ACCESS_KEY": self.config.app_key,
        }
        if isinstance(self.config, R2Config):
            env["AWS_DEFAULT_REGION"] = "auto"
        # Windows only: without these restic can't look up addresses or find a temp folder.
        for name in ("SYSTEMROOT", "TEMP", "TMP"):
            if os.name == "nt" and os.environ.get(name):
                env[name] = os.environ[name]
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
        self.check_key()
        try:
            proc = run_watched([restic_binary(), *args], self._env(), str(cwd) if cwd else None, timeout,
                               self.config.service, self.config.status_page)
        except FileKeepsFailing as exc:
            raise CloudBackupError(CAP_REACHED_MESSAGE if _CAP_RE.search(exc.reason) else self._scrub(str(exc))) from None
        except subprocess.TimeoutExpired as exc:
            raise CloudBackupError(f"{self.config.service} did not respond within {timeout // 60} minutes.") from exc
        if proc.returncode != 0:
            message = self._scrub((proc.stderr or proc.stdout or "").strip())
            lower = message.lower()
            if _CAP_RE.search(message):
                raise CloudBackupError(CAP_REACHED_MESSAGE)
            if any(marker in lower for marker in _MISSING_REPO_MARKERS):
                raise RepositoryMissing("No backups exist in this bucket folder yet.")
            if any(marker in lower for marker in _WRONG_PASSWORD_MARKERS):
                raise WrongRecoveryKey("The recovery key does not match the backups in this bucket folder.")
            if "403" in message or "access denied" in lower or "invalidaccesskeyid" in lower or "signaturedoesnotmatch" in lower:
                raise CloudBackupError(f"{self.config.service} refused the key. Check the key ID, application key and bucket name.")
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
