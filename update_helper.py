"""Small jobs the host updater runs inside the app container with `docker exec`.

    python update_helper.py backup <label>   make and verify the recovery backup
    python update_helper.py checkpoint -       write everything into the database file before a stop
    python update_helper.py health <version> check the running app and its database

Each command prints one JSON object and exits 0 on success, 1 on failure.
It reuses the app's own backup code, so the recovery backup is an ordinary
full backup that also shows up (and can be restored) in Settings.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import app as statement_app

CLOUD_JOB_WAIT_SECONDS = 20 * 60


def verify_full_backup(path: Path) -> dict:
    """Open the backup the same way a restore would, and check everything in it."""
    with tempfile.TemporaryDirectory(dir=statement_app.DATA_DIR) as tmp:
        target = Path(tmp)
        statement_app.extract_backup_archive(path, target)
        manifest_path = target / "manifest.json"
        if not manifest_path.exists():
            raise ValueError("The backup has no manifest.")
        statement_app.validate_backup_manifest(manifest_path)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        statement_app.validate_database_file(target / "firefly_statement.db")
        found = statement_app.count_upload_files(target / "uploads")
        if found != manifest.get("upload_file_count"):
            raise ValueError(f"The backup should hold {manifest.get('upload_file_count')} receipts but holds {found}.")
        return {"upload_file_count": found}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _wait_for_cloud_job() -> None:
    deadline = time.monotonic() + CLOUD_JOB_WAIT_SECONDS
    while statement_app._get_cloud_job().get("state") == "running":
        if time.monotonic() > deadline:
            raise RuntimeError("A cloud backup has been running for too long.")
        time.sleep(5)


def backup(label: str) -> dict:
    with statement_app.app.app_context():
        result = {"cloud_snapshot": ""}
        if statement_app.cloud_backup_configured():
            # The cloud backup goes first, so the local backup also holds its "last backup" time.
            _wait_for_cloud_job()
            cloud = statement_app.run_cloud_backup()
            if not cloud["ok"]:
                raise RuntimeError("Cloud backup failed: " + cloud["error"])
            result["cloud_snapshot"] = cloud["snapshot_id"]
        path = statement_app.create_full_backup_file(label)
        try:
            result.update(verify_full_backup(path))
        except Exception:
            path.unlink(missing_ok=True)
            raise
        result.update({"name": path.name, "path": str(path), "size": path.stat().st_size, "sha256": _sha256(path)})
        return result


def health(expected_version: str) -> dict:
    port = int(os.environ.get("PORT", "5050"))
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=10) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not payload.get("ok"):
        raise RuntimeError("The app says its database is not working.")
    if expected_version and payload.get("version") != expected_version.lstrip("v"):
        raise RuntimeError(f"The app reports version {payload.get('version')}, expected {expected_version.lstrip('v')}.")
    statement_app.validate_database_file(statement_app.DB_PATH)
    return {"version": payload.get("version")}


def checkpoint(_unused: str) -> dict:
    """Fold the write-ahead log into the database file, so nothing is left in a file the container may lose."""
    db = sqlite3.connect(str(statement_app.DB_PATH), timeout=30)
    try:
        busy, _log, _done = db.execute("pragma wal_checkpoint(TRUNCATE)").fetchone()
    finally:
        db.close()
    if busy:
        raise RuntimeError("The database was busy and could not be fully saved to disk.")
    return {}


def main(argv: list[str]) -> int:
    commands = {"backup": backup, "checkpoint": checkpoint, "health": health}
    if len(argv) != 3 or argv[1] not in commands:
        print(json.dumps({"ok": False, "error": "usage: update_helper.py backup <label> | checkpoint - | health <version>"}))
        return 1
    try:
        result = commands[argv[1]](argv[2])
    except (Exception, sqlite3.Error) as exc:
        print(json.dumps({"ok": False, "error": str(exc) or exc.__class__.__name__}))
        return 1
    print(json.dumps({"ok": True, **result}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
