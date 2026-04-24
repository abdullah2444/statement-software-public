#!/usr/bin/env bash
set -Eeuo pipefail

APP_NAME="Statement Software"
SERVICE_NAME="statement-software"
DEFAULT_METHOD="docker"
DEFAULT_PORT="18451"
DEFAULT_HOST="0.0.0.0"
DEFAULT_DATA_DIR="./data"

COMMAND="${1:-help}"
if [[ "$COMMAND" == "--help" || "$COMMAND" == "-h" ]]; then
  COMMAND="help"
else
  shift || true
fi

METHOD=""
PORT=""
HOST=""
DATA_DIR=""
ADMIN_USER="admin"
ADMIN_PASSWORD=""
OPENROUTER_KEY=""
SECURE_COOKIES="0"
MAX_UPLOAD_MB="512"
NON_INTERACTIVE="0"
SKIP_SYSTEM_INSTALL="0"

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

ok() { printf '[OK] %s\n' "$*"; }
warn() { printf '[WARN] %s\n' "$*"; }
fail() { printf '[FAIL] %s\n' "$*"; }
info() { printf '[INFO] %s\n' "$*"; }
die() { fail "$*"; exit 1; }

have_cmd() {
  command -v "$1" >/dev/null 2>&1
}

print_help() {
  cat <<'HELP'
Statement Software setup

Usage:
  bash setup.sh quickstart [options]
  ./setup.sh install [options]
  ./setup.sh start
  ./setup.sh stop
  ./setup.sh restart
  ./setup.sh status
  ./setup.sh doctor
  ./setup.sh backup
  ./setup.sh export
  ./setup.sh restore <backup-file-or-db-file>
  ./setup.sh reset-admin-password [options]
  ./setup.sh --help

Install options:
  --method docker|python       Install method. Default: docker
  --port PORT                  Browser port. Default: 18451
  --host HOST                  Bind address. Default: 0.0.0.0
  --data-dir PATH              Private data folder. Default: ./data
  --admin-user USERNAME        First admin username. Default: admin
  --admin-password PASSWORD    First admin password
  --openrouter-key KEY         Optional OpenRouter API key
  --secure-cookies             Enable secure cookies for HTTPS
  --max-upload-mb MB           Max Settings restore upload size. Default: 512
  --skip-system-install        Do not install missing Docker/Python system packages
  --non-interactive            Do not ask questions; fail if required values are missing

Examples:
  bash setup.sh quickstart
  bash setup.sh quickstart --method docker --port 18451
  ./setup.sh install
  ./setup.sh install --method docker --port 18451
  ./setup.sh install --method python --port 8080 --admin-user admin
  ./setup.sh doctor
  ./setup.sh backup
  ./setup.sh export
  ./setup.sh restore ./data/backups/statement-full-backup-20260424-210000.tar.gz
  ./setup.sh restore ~/Downloads/statement_software.db
  ./setup.sh reset-admin-password --admin-user admin

Safety notes:
  GitHub should store code, templates, static files, scripts, and docs.
  GitHub should NOT store .env, real databases, uploads, backups, logs, or archives.

Beginner note:
  On Ubuntu/Debian Linux, install will try to install missing tools like Docker,
  Docker Compose, Python venv, pip, and PDF dependencies automatically.
HELP
}

parse_options() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --method)
        METHOD="${2:-}"; shift 2 ;;
      --port)
        PORT="${2:-}"; shift 2 ;;
      --host)
        HOST="${2:-}"; shift 2 ;;
      --data-dir)
        DATA_DIR="${2:-}"; shift 2 ;;
      --admin-user)
        ADMIN_USER="${2:-}"; shift 2 ;;
      --admin-password)
        ADMIN_PASSWORD="${2:-}"; shift 2 ;;
      --openrouter-key)
        OPENROUTER_KEY="${2:-}"; shift 2 ;;
      --secure-cookies)
        SECURE_COOKIES="1"; shift ;;
      --max-upload-mb)
        MAX_UPLOAD_MB="${2:-}"; shift 2 ;;
      --skip-system-install)
        SKIP_SYSTEM_INSTALL="1"; shift ;;
      --non-interactive)
        NON_INTERACTIVE="1"; shift ;;
      --help|-h)
        print_help; exit 0 ;;
      *)
        die "Unknown option: $1" ;;
    esac
  done
}

prompt_default() {
  local label="$1"
  local default="$2"
  local value=""
  if [[ "$NON_INTERACTIVE" == "1" ]]; then
    printf '%s' "$default"
    return
  fi
  if [[ -r /dev/tty ]]; then
    read -r -p "$label [$default]: " value </dev/tty
  else
    read -r -p "$label [$default]: " value
  fi
  printf '%s' "${value:-$default}"
}

prompt_secret() {
  local label="$1"
  local value=""
  if [[ "$NON_INTERACTIVE" == "1" ]]; then
    printf '%s' "$ADMIN_PASSWORD"
    return
  fi
  while [[ -z "$value" ]]; do
    if [[ -r /dev/tty ]]; then
      read -r -s -p "$label: " value </dev/tty
      printf '\n' >/dev/tty
    else
      read -r -s -p "$label: " value
      printf '\n'
    fi
    [[ -n "$value" ]] || warn "Password cannot be empty."
  done
  printf '%s' "$value"
}

random_secret() {
  if have_cmd openssl; then
    openssl rand -hex 32
  elif have_cmd python3; then
    python3 - <<'PY'
import secrets
print(secrets.token_hex(32))
PY
  else
    tr -dc 'A-Za-z0-9' </dev/urandom | head -c 64
    printf '\n'
  fi
}

run_as_root() {
  if [[ "${EUID:-$(id -u)}" -eq 0 ]]; then
    "$@"
  elif have_cmd sudo; then
    sudo "$@"
  else
    return 1
  fi
}

apt_install() {
  have_cmd apt-get || return 1
  run_as_root apt-get update
  run_as_root env DEBIAN_FRONTEND=noninteractive apt-get install -y "$@"
}

start_docker_service() {
  if have_cmd systemctl; then
    run_as_root systemctl enable --now docker >/dev/null 2>&1 || true
  elif have_cmd service; then
    run_as_root service docker start >/dev/null 2>&1 || true
  fi
}

compose_cmd() {
  if have_cmd docker && docker compose version >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
    printf 'docker compose'
  elif have_cmd sudo && have_cmd docker && sudo docker info >/dev/null 2>&1 && sudo docker compose version >/dev/null 2>&1; then
    printf 'sudo docker compose'
  elif have_cmd docker-compose && docker info >/dev/null 2>&1; then
    printf 'docker-compose'
  elif have_cmd sudo && sudo docker info >/dev/null 2>&1 && sudo docker-compose version >/dev/null 2>&1; then
    printf 'sudo docker-compose'
  else
    return 1
  fi
}

python_cmd() {
  if have_cmd python3; then
    printf 'python3'
  elif have_cmd python; then
    printf 'python'
  else
    return 1
  fi
}

http_responds() {
  local url="$1"
  if have_cmd curl; then
    curl -fsSL --max-time 5 "$url" >/dev/null 2>&1
    return $?
  fi
  local py
  py="$(python_cmd)" || return 2
  "$py" - "$url" <<'PY' >/dev/null 2>&1
import sys
from urllib.request import urlopen

with urlopen(sys.argv[1], timeout=5) as response:
    if response.status >= 400:
        raise SystemExit(1)
PY
}

port_is_listening() {
  local port="$1"
  if have_cmd ss; then
    ss -ltn 2>/dev/null | grep -q ":$port "
    return $?
  fi
  if have_cmd netstat; then
    netstat -ltn 2>/dev/null | grep -q ":$port "
    return $?
  fi
  return 2
}

load_env() {
  [[ -f .env ]] || die ".env is missing. Run ./setup.sh install first."
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
  METHOD="${INSTALL_METHOD:-${METHOD:-$DEFAULT_METHOD}}"
  PORT="${PORT:-$DEFAULT_PORT}"
  HOST="${HOST:-$DEFAULT_HOST}"
  DATA_DIR="${DATA_DIR:-$DEFAULT_DATA_DIR}"
  DATABASE_PATH="${DATABASE_PATH:-$DATA_DIR/statement_software.db}"
  UPLOAD_DIR="${UPLOAD_DIR:-$DATA_DIR/uploads}"
  BACKUP_DIR="${BACKUP_DIR:-$DATA_DIR/backups}"
  MAX_UPLOAD_MB="${MAX_UPLOAD_MB:-512}"
  FX_PROXY_URL="${FX_PROXY_URL:-}"
  LEGACY_DATABASE_FILENAME="${LEGACY_DATABASE_FILENAME:-}"
}

write_env_file() {
  local secret_key="$1"
  cat > .env <<EOF
INSTALL_METHOD=$METHOD
PORT=$PORT
HOST=$HOST
DATA_DIR=$DATA_DIR

DATABASE_PATH=$DATA_DIR/statement_software.db
UPLOAD_DIR=$DATA_DIR/uploads
BACKUP_DIR=$DATA_DIR/backups
MAX_UPLOAD_MB=$MAX_UPLOAD_MB

APP_NAME="Statement Software"
BRAND_NAME="Statement"
COMPANY_NAME="Your Company"
DEFAULT_PROFIT_EXPENSE_ACCOUNT_NAME="Company Profit"
LEGACY_DATABASE_FILENAME=

SEED_DEMO_DATA=0
SOURCE_CSV_PATH=
DEMO_CLIENT_NAME="Demo Client"

SECRET_KEY=$secret_key
SESSION_COOKIE_SECURE=$SECURE_COOKIES
OPENROUTER_API_KEY=$OPENROUTER_KEY
RESET_SECRET_TOKEN=
FX_PROXY_URL=
EOF
  chmod 600 .env || true
  ok "Wrote .env"
}

ensure_data_dirs() {
  mkdir -p "$DATA_DIR/uploads" "$DATA_DIR/backups"
  touch "$DATA_DIR/statement_software.db"
}

migrate_existing_data() {
  if [[ -s "$DATA_DIR/statement_software.db" ]]; then
    return
  fi
  if [[ -n "${LEGACY_DATABASE_FILENAME:-}" && -f "$LEGACY_DATABASE_FILENAME" ]]; then
    info "Copying existing database into $DATA_DIR"
    cp "$LEGACY_DATABASE_FILENAME" "$DATA_DIR/statement_software.db"
  elif [[ -f statement_software.db ]]; then
    info "Copying existing database into $DATA_DIR"
    cp statement_software.db "$DATA_DIR/statement_software.db"
  fi
}

install_docker_deps() {
  start_docker_service

  if compose_cmd >/dev/null 2>&1; then
    ok "Docker Compose is available"
    return
  fi

  if [[ "$SKIP_SYSTEM_INSTALL" == "1" ]]; then
    die "Docker Compose is missing. Remove --skip-system-install or install Docker manually."
  fi

  if ! have_cmd apt-get; then
    die "Automatic Docker install currently supports Ubuntu/Debian. Install Docker Compose manually or use --method python."
  fi

  info "Docker or Docker Compose is missing. Installing Docker packages with apt."
  if apt_install docker.io docker-compose-plugin; then
    ok "Docker packages installed"
  elif apt_install docker.io docker-compose; then
    ok "Docker packages installed"
  else
    die "Could not install Docker automatically. Try: sudo apt-get install docker.io docker-compose-plugin"
  fi

  start_docker_service

  if [[ -n "${SUDO_USER:-}" && "${SUDO_USER:-}" != "root" ]]; then
    run_as_root usermod -aG docker "$SUDO_USER" >/dev/null 2>&1 || true
    warn "Added $SUDO_USER to the docker group when possible. If Docker asks for permission later, log out and back in."
  fi

  compose_cmd >/dev/null 2>&1 || die "Docker installed, but Docker Compose is still unavailable. Log out/in, then run ./setup.sh install again."
}

install_python_system_deps() {
  local py
  if py="$(python_cmd)" && "$py" -m venv --help >/dev/null 2>&1; then
    ok "Python and venv are available"
    return
  fi

  if [[ "$SKIP_SYSTEM_INSTALL" == "1" ]]; then
    die "Python/venv is missing. Remove --skip-system-install or install Python packages manually."
  fi

  if ! have_cmd apt-get; then
    die "Automatic Python dependency install currently supports Ubuntu/Debian. Install python3-venv python3-pip manually."
  fi

  info "Python system packages are missing. Installing Python, venv, pip, and PDF dependencies with apt."
  apt_install \
    python3 python3-venv python3-pip \
    libpango-1.0-0 libpangocairo-1.0-0 libgdk-pixbuf-2.0-0 \
    libffi-dev libcairo2 libglib2.0-0 fonts-dejavu-core \
    || die "Could not install Python dependencies automatically."
}

install_python_deps() {
  install_python_system_deps
  local py
  py="$(python_cmd)" || die "Python is required for Python mode."
  "$py" -m venv .venv
  .venv/bin/python -m pip install --upgrade pip
  .venv/bin/pip install -r requirements.txt
  ok "Python dependencies installed"
}

init_admin_python() {
  local python_bin="$1"
  INITIAL_ADMIN_USERNAME="$ADMIN_USER" \
  INITIAL_ADMIN_PASSWORD="$ADMIN_PASSWORD" \
  INITIAL_ADMIN_MUST_CHANGE="0" \
  DATABASE_PATH="$DATA_DIR/statement_software.db" \
  UPLOAD_DIR="$DATA_DIR/uploads" \
  BACKUP_DIR="$DATA_DIR/backups" \
  SECRET_KEY="${SECRET_KEY:-}" \
  "$python_bin" - <<'PY'
import sqlite3
import app

existing_users = 0
if app.DB_PATH.exists():
    try:
        probe = sqlite3.connect(app.DB_PATH)
        has_users = probe.execute(
            "select count(*) from sqlite_master where type = 'table' and name = 'users'"
        ).fetchone()[0]
        if has_users:
            existing_users = probe.execute("select count(*) from users").fetchone()[0]
        probe.close()
    except sqlite3.DatabaseError:
        existing_users = 0
app.init_db()
db = sqlite3.connect(app.DB_PATH)
count = db.execute("select count(*) from users").fetchone()[0]
db.close()
print(f"[setup] users in database: {count}")
if existing_users:
    print("[setup] existing users were found; installer admin password was not applied.")
PY
}

init_admin_docker() {
  local compose
  compose="$(compose_cmd)" || die "Docker Compose is required for Docker mode."
  $compose run --rm -T \
    -e INITIAL_ADMIN_USERNAME="$ADMIN_USER" \
    -e INITIAL_ADMIN_PASSWORD="$ADMIN_PASSWORD" \
    -e INITIAL_ADMIN_MUST_CHANGE="0" \
    "$SERVICE_NAME" python - <<'PY'
import sqlite3
import app

existing_users = 0
if app.DB_PATH.exists():
    try:
        probe = sqlite3.connect(app.DB_PATH)
        has_users = probe.execute(
            "select count(*) from sqlite_master where type = 'table' and name = 'users'"
        ).fetchone()[0]
        if has_users:
            existing_users = probe.execute("select count(*) from users").fetchone()[0]
        probe.close()
    except sqlite3.DatabaseError:
        existing_users = 0
app.init_db()
db = sqlite3.connect(app.DB_PATH)
count = db.execute("select count(*) from users").fetchone()[0]
db.close()
print(f"[setup] users in database: {count}")
if existing_users:
    print("[setup] existing users were found; installer admin password was not applied.")
PY
}

reset_admin_password_python() {
  local python_bin="$1"
  RESET_ADMIN_USERNAME="$ADMIN_USER" \
  RESET_ADMIN_PASSWORD="$ADMIN_PASSWORD" \
  DATABASE_PATH="$DATA_DIR/statement_software.db" \
  UPLOAD_DIR="$DATA_DIR/uploads" \
  BACKUP_DIR="$DATA_DIR/backups" \
  SECRET_KEY="${SECRET_KEY:-}" \
  "$python_bin" - <<'PY'
import os
import sqlite3
from datetime import datetime, timezone

from werkzeug.security import generate_password_hash

import app

username = (os.environ.get("RESET_ADMIN_USERNAME") or "admin").strip()
password = os.environ.get("RESET_ADMIN_PASSWORD") or ""
if not username or not password:
    raise SystemExit("RESET_ADMIN_USERNAME and RESET_ADMIN_PASSWORD are required")

app.init_db()
db = sqlite3.connect(app.DB_PATH)
existing = db.execute("select id from users where username = ?", (username,)).fetchone()
password_hash = generate_password_hash(password)
if existing:
    db.execute(
        """
        update users
        set password_hash = ?, role = 'admin', is_active = 1, must_change_password = 0
        where username = ?
        """,
        (password_hash, username),
    )
    print(f"[setup] admin password reset for '{username}'.")
else:
    db.execute(
        """
        insert into users(username, password_hash, role, is_active, must_change_password, created_at)
        values (?, ?, 'admin', 1, 0, ?)
        """,
        (username, password_hash, datetime.now(timezone.utc).isoformat()),
    )
    print(f"[setup] admin user '{username}' created.")
db.commit()
db.close()
PY
}

reset_admin_password() {
  parse_options "$@"
  load_env
  ADMIN_USER="${ADMIN_USER:-admin}"
  if [[ -z "$ADMIN_PASSWORD" ]]; then
    ADMIN_PASSWORD="$(prompt_secret "New password for '$ADMIN_USER'")"
  fi
  [[ -n "$ADMIN_PASSWORD" ]] || die "--admin-password is required in non-interactive mode."

  if [[ "$METHOD" == "docker" ]]; then
    local compose
    compose="$(compose_cmd)" || die "Docker Compose is required for Docker mode."
    $compose run --rm -T \
      -e RESET_ADMIN_USERNAME="$ADMIN_USER" \
      -e RESET_ADMIN_PASSWORD="$ADMIN_PASSWORD" \
      "$SERVICE_NAME" python - <<'PY'
import os
import sqlite3
from datetime import datetime, timezone

from werkzeug.security import generate_password_hash

import app

username = (os.environ.get("RESET_ADMIN_USERNAME") or "admin").strip()
password = os.environ.get("RESET_ADMIN_PASSWORD") or ""
if not username or not password:
    raise SystemExit("RESET_ADMIN_USERNAME and RESET_ADMIN_PASSWORD are required")

app.init_db()
db = sqlite3.connect(app.DB_PATH)
existing = db.execute("select id from users where username = ?", (username,)).fetchone()
password_hash = generate_password_hash(password)
if existing:
    db.execute(
        """
        update users
        set password_hash = ?, role = 'admin', is_active = 1, must_change_password = 0
        where username = ?
        """,
        (password_hash, username),
    )
    print(f"[setup] admin password reset for '{username}'.")
else:
    db.execute(
        """
        insert into users(username, password_hash, role, is_active, must_change_password, created_at)
        values (?, ?, 'admin', 1, 0, ?)
        """,
        (username, password_hash, datetime.now(timezone.utc).isoformat()),
    )
    print(f"[setup] admin user '{username}' created.")
db.commit()
db.close()
PY
  else
    [[ -x .venv/bin/python ]] || die ".venv is missing. Run ./setup.sh install --method python first."
    reset_admin_password_python ".venv/bin/python"
  fi

  ok "Admin password is ready for username '$ADMIN_USER'"
  info "Restart with: ./setup.sh restart"
}

install_app() {
  parse_options "$@"
  METHOD="${METHOD:-$(prompt_default "Install method: docker or python" "$DEFAULT_METHOD")}"
  PORT="${PORT:-$(prompt_default "Browser port" "$DEFAULT_PORT")}"
  HOST="${HOST:-$(prompt_default "Bind host" "$DEFAULT_HOST")}"
  DATA_DIR="${DATA_DIR:-$(prompt_default "Private data folder" "$DEFAULT_DATA_DIR")}"
  ADMIN_USER="${ADMIN_USER:-admin}"
  if [[ -z "$ADMIN_PASSWORD" ]]; then
    ADMIN_PASSWORD="$(prompt_secret "First admin password for '$ADMIN_USER'")"
  fi
  [[ "$METHOD" == "docker" || "$METHOD" == "python" ]] || die "--method must be docker or python."
  [[ "$PORT" =~ ^[0-9]+$ ]] || die "--port must be a number."
  [[ "$MAX_UPLOAD_MB" =~ ^[0-9]+$ && "$MAX_UPLOAD_MB" -gt 0 ]] || die "--max-upload-mb must be a positive number."
  [[ -n "$ADMIN_PASSWORD" ]] || die "--admin-password is required in non-interactive mode."

  local secret_key
  secret_key="$(random_secret)"
  write_env_file "$secret_key"
  load_env
  ensure_data_dirs
  migrate_existing_data

  if [[ "$METHOD" == "docker" ]]; then
    local compose
    install_docker_deps
    compose="$(compose_cmd)" || die "Docker Compose is required for Docker mode."
    $compose build
    init_admin_docker
  else
    install_python_deps
    init_admin_python ".venv/bin/python"
  fi

  ok "Install complete"
  info "Start with: ./setup.sh start"
  info "Open: http://localhost:$PORT"
}

start_app() {
  load_env
  ensure_data_dirs
  if [[ "$METHOD" == "docker" ]]; then
    local compose
    compose="$(compose_cmd)" || die "Docker Compose is required for Docker mode."
    info "Recreating Docker container"
    $compose down --remove-orphans >/dev/null 2>&1 || true
    $compose up -d --force-recreate
  else
    [[ -x .venv/bin/python ]] || die ".venv is missing. Run ./setup.sh install --method python first."
    if [[ -f "$DATA_DIR/app.pid" ]] && kill -0 "$(cat "$DATA_DIR/app.pid")" >/dev/null 2>&1; then
      ok "App is already running with PID $(cat "$DATA_DIR/app.pid")"
      return
    fi
    DATABASE_PATH="$DATABASE_PATH" UPLOAD_DIR="$UPLOAD_DIR" BACKUP_DIR="$BACKUP_DIR" MAX_UPLOAD_MB="${MAX_UPLOAD_MB:-512}" HOST="$HOST" PORT="$PORT" SECRET_KEY="${SECRET_KEY:-}" SESSION_COOKIE_SECURE="${SESSION_COOKIE_SECURE:-0}" OPENROUTER_API_KEY="${OPENROUTER_API_KEY:-}" RESET_SECRET_TOKEN="${RESET_SECRET_TOKEN:-}" FX_PROXY_URL="${FX_PROXY_URL:-}" \
      nohup .venv/bin/python app.py > "$DATA_DIR/app.out.log" 2> "$DATA_DIR/app.err.log" &
    echo "$!" > "$DATA_DIR/app.pid"
  fi
  ok "Started $APP_NAME"
  info "Open: http://127.0.0.1:$PORT"
}

stop_app() {
  load_env
  if [[ "$METHOD" == "docker" ]]; then
    local compose
    compose="$(compose_cmd)" || die "Docker Compose is required for Docker mode."
    $compose down
  else
    if [[ -f "$DATA_DIR/app.pid" ]] && kill -0 "$(cat "$DATA_DIR/app.pid")" >/dev/null 2>&1; then
      kill "$(cat "$DATA_DIR/app.pid")"
      rm -f "$DATA_DIR/app.pid"
      ok "Stopped $APP_NAME"
    else
      warn "App is not running"
    fi
  fi
}

status_app() {
  load_env
  if [[ "$METHOD" == "docker" ]]; then
    local compose
    compose="$(compose_cmd)" || die "Docker Compose is required for Docker mode."
    $compose ps
  else
    if [[ -f "$DATA_DIR/app.pid" ]] && kill -0 "$(cat "$DATA_DIR/app.pid")" >/dev/null 2>&1; then
      ok "Running with PID $(cat "$DATA_DIR/app.pid")"
    else
      warn "Not running"
    fi
  fi
}

show_docker_diagnostics() {
  local compose
  compose="$(compose_cmd)" || return 0
  info "Docker container status:"
  $compose ps || true
  info "Recent Docker logs:"
  $compose logs --tail=80 "$SERVICE_NAME" || true
}

doctor_app() {
  load_env
  local failures=0
  local url="http://127.0.0.1:$PORT"

  [[ -f app.py ]] && ok "app.py found" || { fail "app.py missing"; failures=$((failures + 1)); }
  [[ -f .env ]] && ok ".env found" || { fail ".env missing"; failures=$((failures + 1)); }
  [[ -d "$DATA_DIR" ]] && ok "data folder exists: $DATA_DIR" || { fail "data folder missing: $DATA_DIR"; failures=$((failures + 1)); }
  [[ -f "$DATABASE_PATH" ]] && ok "database exists: $DATABASE_PATH" || { fail "database missing: $DATABASE_PATH"; failures=$((failures + 1)); }
  [[ -d "$UPLOAD_DIR" ]] && ok "uploads folder exists" || warn "uploads folder missing"
  [[ -d "$BACKUP_DIR" ]] && ok "backups folder exists" || warn "backups folder missing"

  if [[ "$METHOD" == "docker" ]]; then
    compose_cmd >/dev/null 2>&1 && ok "Docker Compose available" || { fail "Docker Compose missing"; failures=$((failures + 1)); }
    local compose
    if compose="$(compose_cmd)"; then
      $compose ps
    fi
  else
    [[ -x .venv/bin/python ]] && ok "Python virtualenv available" || { fail ".venv missing"; failures=$((failures + 1)); }
  fi

  local py
  if py="$(python_cmd)"; then
    if DATABASE_PATH="$DATABASE_PATH" "$py" - <<'PY' >/dev/null 2>&1; then
import os, sqlite3
db = sqlite3.connect(os.environ["DATABASE_PATH"])
db.execute("select name from sqlite_master limit 1").fetchall()
db.close()
PY
      ok "database opens successfully"
    else
      fail "database cannot be opened"
      failures=$((failures + 1))
    fi
  else
    warn "Python is not available for database check"
  fi

  local http_status=1
  if http_responds "$url"; then
    http_status=0
    ok "app responds at $url"
  else
    warn "app is not responding at $url"
    if [[ "$METHOD" == "docker" ]]; then
      show_docker_diagnostics
    fi
  fi

  local port_status=1
  if port_is_listening "$PORT"; then
    port_status=0
    ok "port $PORT is listening"
  else
    port_status=$?
    if [[ "$http_status" -eq 0 ]]; then
      ok "port $PORT is reachable by HTTP"
    elif [[ "$port_status" -eq 2 ]]; then
      warn "ss/netstat unavailable; skipped low-level port check"
    else
      warn "port $PORT is not listening"
    fi
  fi

  if compgen -G "$BACKUP_DIR/statement-full-backup-*.tar.gz" >/dev/null || compgen -G "$BACKUP_DIR/*.tgz" >/dev/null || compgen -G "$BACKUP_DIR/*.db" >/dev/null; then
    ok "at least one backup exists"
  else
    warn "no backups found yet; run ./setup.sh backup"
  fi

  if [[ "$failures" -gt 0 ]]; then
    fail "Doctor found $failures problem(s). Fix them, then run ./setup.sh doctor again."
    exit 1
  fi
  ok "Doctor finished. Next step: use ./setup.sh start or open $url"
}

backup_app() {
  load_env
  ensure_data_dirs
  local stamp
  stamp="$(date +%Y%m%d-%H%M%S)"
  local backup_file="$BACKUP_DIR/statement-full-backup-$stamp.tar.gz"
  local py
  py="$(python_cmd)" || die "Python is required for backup."
  DATABASE_PATH="$DATABASE_PATH" UPLOAD_DIR="$UPLOAD_DIR" BACKUP_FILE="$backup_file" "$py" - <<'PY'
import json, os, sqlite3, tarfile, tempfile
from datetime import datetime, timezone
from pathlib import Path

db_path = Path(os.environ["DATABASE_PATH"])
upload_dir = Path(os.environ["UPLOAD_DIR"])
backup_file = Path(os.environ["BACKUP_FILE"])
backup_file.parent.mkdir(parents=True, exist_ok=True)

def upload_file_count(path):
    if not path.exists():
        return 0
    return sum(1 for item in path.rglob("*") if item.is_file())

with tempfile.TemporaryDirectory() as tmp:
    tmp_path = Path(tmp)
    db_copy = tmp_path / "statement_software.db"
    manifest = tmp_path / "manifest.json"
    src = sqlite3.connect(db_path)
    dst = sqlite3.connect(db_copy)
    src.backup(dst)
    dst.close()
    src.close()
    uploads_source = upload_dir
    if not uploads_source.exists():
        uploads_source = tmp_path / "uploads"
        uploads_source.mkdir()
    manifest.write_text(json.dumps({
        "format_version": 1,
        "app_name": os.environ.get("APP_NAME", "Statement Software"),
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "database_file": "statement_software.db",
        "uploads_dir": "uploads",
        "upload_file_count": upload_file_count(uploads_source),
        "notes": "Statement Software full backup. Includes SQLite database and uploads only.",
    }, indent=2), encoding="utf-8")
    with tarfile.open(backup_file, "w:gz") as tar:
        tar.add(manifest, arcname="manifest.json")
        tar.add(db_copy, arcname="statement_software.db")
        tar.add(uploads_source, arcname="uploads")
print(backup_file)
PY
  ok "Full backup created: $backup_file"
}

restore_app() {
  local restore_source="${1:-}"
  [[ -n "$restore_source" ]] || die "Usage: ./setup.sh restore <backup-file-or-db-file>"
  [[ -f "$restore_source" ]] || die "Restore file not found: $restore_source"
  load_env
  backup_app
  stop_app || true
  local py
  py="$(python_cmd)" || die "Python is required for restore."
  RESTORE_SOURCE="$restore_source" DATABASE_PATH="$DATABASE_PATH" UPLOAD_DIR="$UPLOAD_DIR" "$py" - <<'PY'
import json, os, shutil, sqlite3, tarfile, tempfile
from pathlib import Path

restore_source = Path(os.environ["RESTORE_SOURCE"])
db_path = Path(os.environ["DATABASE_PATH"])
upload_dir = Path(os.environ["UPLOAD_DIR"])
allowed_db_ext = {".db", ".sqlite", ".sqlite3"}


def validate_database(path):
    db = sqlite3.connect(path)
    try:
        integrity = db.execute("pragma integrity_check").fetchone()
        if not integrity or str(integrity[0]).lower() != "ok":
            raise SystemExit("database integrity check failed")
        tables = {row[0] for row in db.execute("select name from sqlite_master where type='table'")}
        required = {"users", "clients", "statement_entries"}
        missing = sorted(required - tables)
        if missing:
            raise SystemExit(f"database is missing required tables: {', '.join(missing)}")
    finally:
        db.close()


def safe_extract_archive(archive_path, target_dir):
    try:
        with tarfile.open(archive_path, "r:gz") as tar:
            base = target_dir.resolve()
            for member in tar.getmembers():
                if member.issym() or member.islnk():
                    raise SystemExit(f"backup archive contains unsafe link: {member.name}")
                target = (target_dir / member.name).resolve()
                if not target.is_relative_to(base):
                    raise SystemExit(f"unsafe backup path: {member.name}")
            tar.extractall(target_dir)
    except tarfile.TarError as exc:
        raise SystemExit(f"restore archive is not valid: {exc}")


def validate_manifest(path):
    if not path.exists():
        return
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise SystemExit(f"backup manifest is not valid: {exc}")
    if manifest.get("format_version") != 1:
        raise SystemExit("backup manifest format is not supported")


def copy_database(source_path, target_path):
    validate_database(source_path)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(source_path)
    dst = sqlite3.connect(target_path)
    try:
        with dst:
            src.backup(dst)
    finally:
        dst.close()
        src.close()


with tempfile.TemporaryDirectory() as tmp:
    tmp_path = Path(tmp)
    restored_uploads = None
    if tarfile.is_tarfile(restore_source):
        safe_extract_archive(restore_source, tmp_path)
        validate_manifest(tmp_path / "manifest.json")
        manifest_path = tmp_path / "manifest.json"
        restored_db = tmp_path / "statement_software.db"
        if manifest_path.exists():
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                manifest_database = str(manifest.get("database_file") or "").strip()
                if manifest_database:
                    restored_db = tmp_path / manifest_database
            except Exception:
                pass
        legacy_database = os.environ.get("LEGACY_DATABASE_FILENAME", "").strip()
        if not restored_db.exists() and legacy_database:
            restored_db = tmp_path / legacy_database
        if not restored_db.exists():
            raise SystemExit("backup does not contain statement_software.db")
        restored_uploads = tmp_path / "uploads"
        restored_uploads.mkdir(exist_ok=True)
    elif restore_source.suffix.lower() in allowed_db_ext:
        restored_db = restore_source
        print("[WARN] Restoring a database-only file; uploaded images are not included.")
    else:
        raise SystemExit("restore file must be a .tar.gz/.tgz backup or a .db/.sqlite/.sqlite3 database")

    copy_database(restored_db, db_path)
    if restored_uploads and restored_uploads.exists():
        if upload_dir.exists():
            shutil.rmtree(upload_dir)
        shutil.copytree(restored_uploads, upload_dir)
print("restore complete")
PY
  start_app
  ok "Restore complete"
}

quickstart_app() {
  info "Quickstart will install dependencies, create config, start the app, and run doctor."
  install_app "$@"
  start_app
  doctor_app
}

case "$COMMAND" in
  help)
    print_help ;;
  quickstart|setup|all)
    quickstart_app "$@" ;;
  install)
    install_app "$@" ;;
  start)
    start_app ;;
  stop)
    stop_app ;;
  restart)
    stop_app || true
    start_app ;;
  status)
    status_app ;;
  doctor)
    doctor_app ;;
  backup|export)
    backup_app ;;
  restore)
    restore_app "${1:-}" ;;
  reset-admin-password|reset-admin)
    reset_admin_password "$@" ;;
  *)
    fail "Unknown command: $COMMAND"
    print_help
    exit 1 ;;
esac
