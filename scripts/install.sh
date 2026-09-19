#!/usr/bin/env bash
set -Eeuo pipefail

REPO_URL="https://github.com/abdullah2444/statement-software-public.git"
BRANCH="main"
INSTALL_DIR="$HOME/statement-software-public"
BIN_DIR="$HOME/.local/bin"
SKIP_SETUP="0"
AUTO_START="1"
SETUP_ARGS=()

print_help() {
  cat <<'HELP'
Statement Software v5 - One-Line Installer

🚀 Quick Install (recommended):
  curl -fsSL https://raw.githubusercontent.com/abdullah2444/statement-software-public/main/scripts/install.sh | bash

📦 Install with options:
  curl -fsSL https://raw.githubusercontent.com/abdullah2444/statement-software-public/main/scripts/install.sh | bash -s -- [options]

🔄 Update existing installation:
  curl -fsSL https://raw.githubusercontent.com/abdullah2444/statement-software-public/main/scripts/install.sh | bash
  (Automatically detects and upgrades v4 → v5 with data migration)

Options:
  --dir PATH             Install directory. Default: ~/statement-software-public
  --branch NAME          Git branch. Default: main
  --repo URL             Git repository URL
  --skip-setup           Clone/update code only, don't run setup
  --no-start             Setup but don't auto-start the service
  --help                 Show this help

Setup options (passed through to setup.sh):
  --method docker|python
  --port PORT            Default: 18451
  --host HOST            Default: 0.0.0.0
  --admin-password PWD   Required for first install
  --non-interactive      No prompts (auto-generates password if needed)

Examples:
  # Install with Docker (default) and custom port
  curl -fsSL https://raw.githubusercontent.com/abdullah2444/statement-software-public/main/scripts/install.sh | \
    bash -s -- --port 8080

  # Install with Python mode and set admin password
  curl -fsSL https://raw.githubusercontent.com/abdullah2444/statement-software-public/main/scripts/install.sh | \
    bash -s -- --method python --admin-password MySecurePass123

  # Update only (no restart)
  curl -fsSL https://raw.githubusercontent.com/abdullah2444/statement-software-public/main/scripts/install.sh | \
    bash -s -- --skip-setup

After install, access at: http://YOUR_SERVER_IP:18451
HELP
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dir)
      INSTALL_DIR="${2:-}"
      shift 2
      ;;
    --branch)
      BRANCH="${2:-main}"
      shift 2
      ;;
    --repo)
      REPO_URL="${2:-}"
      shift 2
      ;;
    --skip-setup)
      SKIP_SETUP="1"
      shift
      ;;
    --no-start)
      AUTO_START="0"
      shift
      ;;
    --help|-h)
      print_help
      exit 0
      ;;
    *)
      SETUP_ARGS+=("$1")
      shift
      ;;
  esac
done

ok() { printf '[OK] %s\n' "$*"; }
info() { printf '[INFO] %s\n' "$*"; }
fail() { printf '[FAIL] %s\n' "$*" >&2; }

have_cmd() {
  command -v "$1" >/dev/null 2>&1
}

sudo_cmd() {
  if [[ "$(id -u)" -eq 0 ]]; then
    "$@"
  elif have_cmd sudo; then
    sudo "$@"
  else
    fail "sudo is required to install missing system packages."
    exit 1
  fi
}

install_git_if_missing() {
  if have_cmd git; then
    return
  fi
  if have_cmd apt-get; then
    info "Installing git"
    sudo_cmd apt-get update
    sudo_cmd apt-get install -y git ca-certificates curl
  else
    fail "git is required. Install git first, then rerun this installer."
    exit 1
  fi
}

install_git_if_missing
mkdir -p "$(dirname "$INSTALL_DIR")" "$BIN_DIR"

IS_UPDATE="0"
if [[ -d "$INSTALL_DIR/.git" ]]; then
  IS_UPDATE="1"
  info "Updating existing installation at $INSTALL_DIR"
  OLD_COMMIT=$(git -C "$INSTALL_DIR" rev-parse HEAD 2>/dev/null || echo "unknown")
  git -C "$INSTALL_DIR" fetch origin "$BRANCH"
  git -C "$INSTALL_DIR" checkout "$BRANCH"
  git -C "$INSTALL_DIR" pull --ff-only origin "$BRANCH"
  NEW_COMMIT=$(git -C "$INSTALL_DIR" rev-parse HEAD 2>/dev/null || echo "unknown")
  if [[ "$OLD_COMMIT" != "$NEW_COMMIT" ]]; then
    ok "Updated from ${OLD_COMMIT:0:7} to ${NEW_COMMIT:0:7}"
  else
    ok "Already on latest version (${NEW_COMMIT:0:7})"
  fi
elif [[ -e "$INSTALL_DIR" ]]; then
  fail "$INSTALL_DIR already exists but is not a Git checkout."
  fail "Move it away or choose another directory with --dir PATH."
  exit 1
else
  info "Installing Statement Software v5 to $INSTALL_DIR"
  git clone --branch "$BRANCH" "$REPO_URL" "$INSTALL_DIR"
  ok "Cloned repository"
fi

chmod +x "$INSTALL_DIR/setup.sh" 2>/dev/null || true
[[ -f "$INSTALL_DIR/scripts/statementsw" ]] && chmod +x "$INSTALL_DIR/scripts/statementsw"

if [[ "$SKIP_SETUP" == "1" ]]; then
  ok "Code updated. Run: cd $INSTALL_DIR && bash setup.sh start"
  exit 0
fi

# For fresh installs, run quickstart. For updates, just restart.
cd "$INSTALL_DIR"
if [[ "$IS_UPDATE" == "1" ]]; then
  info "Restarting service with updated code..."
  bash setup.sh stop 2>/dev/null || true
  if [[ "$AUTO_START" == "1" ]]; then
    bash setup.sh start
    ok "Statement Software v5 updated and restarted"
  else
    ok "Statement Software v5 updated (not started)"
  fi
else
  # Fresh install - run quickstart with passed args
  info "Running initial setup..."
  bash setup.sh quickstart "${SETUP_ARGS[@]}"
  ok "Statement Software v5 installed and started"
fi

# Print access info
if [[ -f .env ]]; then
  PORT=$(grep "^PORT=" .env 2>/dev/null | cut -d= -f2 || echo "18451")
  printf '\n'
  printf '========================================\n'
  printf '✅ Statement Software v5 Ready!\n'
  printf '========================================\n'
  printf 'Access: http://YOUR_SERVER_IP:%s/\n' "$PORT"
  printf 'Manage: cd %s && bash setup.sh status\n' "$INSTALL_DIR"
  printf '========================================\n'
fi
