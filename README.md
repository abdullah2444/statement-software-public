# Statement Software v5

Private statement management software for Firefly Trading.

## 🚀 Quick Install

**One-line install (fresh install or update):**

```bash
curl -fsSL https://raw.githubusercontent.com/abdullah2444/statement-software-public/main/scripts/install.sh | bash
```

That's it! The installer will:
- ✅ Install or update to the latest v5
- ✅ Auto-migrate v4 → v5 data (if upgrading)
- ✅ Start the service automatically
- ✅ Access at `http://YOUR_SERVER_IP:18451`

**Custom installation:**

```bash
# Custom port
curl -fsSL https://raw.githubusercontent.com/abdullah2444/statement-software-public/main/scripts/install.sh | \
  bash -s -- --port 8080 --admin-password YourSecurePass123

# Python mode instead of Docker
curl -fsSL https://raw.githubusercontent.com/abdullah2444/statement-software-public/main/scripts/install.sh | \
  bash -s -- --method python
```

---

## About

This project is meant to keep code in GitHub and keep real business data on the server.

## What Belongs In GitHub

Commit these files:

- `app.py`
- `templates/`
- `static/`
- `requirements.txt`
- `Dockerfile`
- `docker-compose.yml`
- `setup.sh`
- `.env.example`
- `.gitignore`
- documentation

Do not commit these files:

- `.env`
- `data/`
- database files such as `firefly_statement.db`
- uploads
- backups
- logs
- `.tar.gz` backup archives
- old extracted duplicate folders

## Quick Install On Linux

From a fresh clone:

```bash
bash setup.sh quickstart
```

Then open:

```text
http://localhost:18451
```

Docker is the recommended install method. Python mode is also available.

On Ubuntu/Debian Linux, the installer tries to install missing beginner dependencies for you:

- Docker and Docker Compose for Docker mode.
- Python, venv, pip, and PDF export libraries for Python mode.

If you do not want the script to install system packages, use:

```bash
./setup.sh install --skip-system-install
```

## Useful Commands

```bash
bash setup.sh quickstart
./setup.sh --help
./setup.sh install
./setup.sh start
./setup.sh stop
./setup.sh restart
./setup.sh status
./setup.sh doctor
./setup.sh backup
./setup.sh export
./setup.sh restore ./data/backups/statement-full-backup-YYYYMMDD-HHMMSS.tar.gz
./setup.sh reset-admin-password --admin-user admin
```

## API

The app includes a JSON API for integrations and future mobile apps. It supports normal username/password login for people and API tokens for tools.

- Beginner guide: `docs/API.md`
- OpenAPI file: `docs/openapi.yaml`

API token access levels are simple: `full_control`, `read_only`, and `client_portal`.

## Automated GitHub Backups

Set up automatic backups every 30 minutes of your database to a private GitHub repository:

```bash
bash scripts/setup-github-backup.sh
```

**Features:**
- 🔄 **Automatic backups every 30 minutes** via cron
- 📦 **Version history** - every backup is a Git commit
- 🔒 **Private repository** - your data stays secure
- 🗄️ **Snapshot cleanup** - keeps the last 336 snapshots in the working tree (about 7 days); older snapshots remain in Git history
- ⚡ **One-time setup** - wizard guides you through configuration

**What you need:**
1. A private GitHub repository (e.g., `username/statement-backups`)
2. A GitHub Personal Access Token with `repo` scope

**Update an existing installation’s schedule:**

```bash
git pull
bash scripts/setup-github-backup.sh --schedule-only
```

Run this on each Linux machine that needs backups. Each installation with separate data should use its own private backup repository. The machine must be running and connected to the internet at backup time; cron does not catch up while it is powered off.

**Manual backup:**
```bash
bash scripts/backup-to-github.sh
```

**View backup logs:**
```bash
tail -f data/backup-github.log
```

## Install Examples

Interactive Docker install:

```bash
bash setup.sh quickstart
```

Docker install on a custom port:

```bash
bash setup.sh quickstart --method docker --port 8080
```

Direct Python install:

```bash
bash setup.sh quickstart --method python --port 18451
```

Non-interactive install:

```bash
bash setup.sh quickstart --method docker --port 18451 --admin-user admin --admin-password 'change-this-password' --non-interactive
```

## Configuration

`setup.sh install` creates `.env` for you. `.env` is private and ignored by Git.

Important settings:

- `PORT`: browser port.
- `HOST`: bind address.
- `DATA_DIR`: private runtime data folder.
- `DATABASE_PATH`: SQLite database path.
- `UPLOAD_DIR`: uploaded image/file path.
- `BACKUP_DIR`: backup path.
- `MAX_UPLOAD_MB`: max Settings restore upload size. Default: `512`.
- `SEED_DEMO_DATA`: keep `0` for normal fresh installs; use `1` only if you have a demo CSV.
- `SOURCE_CSV_PATH`: optional path to that demo CSV when `SEED_DEMO_DATA=1`.
- `SECRET_KEY`: private Flask session secret.
- `SESSION_COOKIE_SECURE`: use `1` only when serving through HTTPS.
- `OPENROUTER_API_KEY`: optional AI image parsing key.

## Backups

Create a full backup:

```bash
./setup.sh backup
```

This creates a private `.tar.gz` package with:

- `manifest.json`
- `firefly_statement.db`
- `uploads/`

This full backup is the best way to move the app data to another machine because it includes uploaded images.

Restore a full backup:

```bash
./setup.sh restore ./data/backups/statement-full-backup-YYYYMMDD-HHMMSS.tar.gz
```

Restore a raw SQLite database file from an older export:

```bash
./setup.sh restore ~/Downloads/firefly_statement.db
```

Raw `.db` restores are database-only and do not include uploaded images. Restore automatically makes a full safety backup before replacing current data.

GitHub stores code only. Full backups, databases, uploads, logs, and `.env` stay private and must not be committed.

## Health Check

Run:

```bash
./setup.sh doctor
```

The doctor command checks the environment, config, database, data folders, port, app response, and backups. It prints `[OK]`, `[WARN]`, and `[FAIL]` messages so the next step is clear.

## Reset Login Password

If setup says the password is wrong but the app already has an admin user, reset it explicitly:

```bash
./setup.sh reset-admin-password --admin-user admin
./setup.sh restart
```

The reset command asks for a new password and keeps the existing database.

## Updating From GitHub

Before updating:

```bash
./setup.sh backup
```

Then pull the latest code:

```bash
git pull
./setup.sh doctor
./setup.sh restart
```
