#!/usr/bin/env bash
set -Eeuo pipefail

REPO_URL="https://gitee.com/abdullah24/statement-software-public.git"
BRANCH="main"
INSTALL_DIR="$HOME/.statement-software/statement-software-public"
BIN_DIR="$HOME/.local/bin"
SKIP_SETUP="0"
SETUP_ARGS=()

print_help() {
  cat <<'HELP'
Statement Software installer

Usage:
  git clone https://gitee.com/abdullah24/statement-software-public.git
  cd statement-software-public
  bash setup.sh quickstart

Optional GitHub raw installer:
  curl -fsSL https://raw.githubusercontent.com/abdullah2444/statement-software-public/main/scripts/install.sh | bash
  curl -fsSL https://raw.githubusercontent.com/abdullah2444/statement-software-public/main/scripts/install.sh | bash -s -- [options]

Options:
  --dir PATH             Install directory. Default: ~/.statement-software/statement-software-public
  --branch NAME          Git branch. Default: main
  --repo URL             Git repository URL. Default: Gitee mirror
  --skip-setup           Install the statementsw command but do not run setup
  --help                 Show this help

Setup options passed through:
  --method docker|python
  --port PORT
  --host HOST
  --data-dir PATH
  --admin-user USERNAME
  --admin-password PASSWORD
  --openrouter-key KEY
  --secure-cookies
  --max-upload-mb MB
  --skip-system-install
  --non-interactive

After install:
  statementsw setup
  statementsw status
  statementsw doctor
  statementsw backup
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

if [[ -d "$INSTALL_DIR/.git" ]]; then
  info "Updating existing install at $INSTALL_DIR"
  git -C "$INSTALL_DIR" fetch origin "$BRANCH"
  git -C "$INSTALL_DIR" checkout "$BRANCH"
  git -C "$INSTALL_DIR" pull --ff-only origin "$BRANCH"
elif [[ -e "$INSTALL_DIR" ]]; then
  fail "$INSTALL_DIR already exists but is not a Git checkout."
  fail "Move it away or choose another directory with --dir PATH."
  exit 1
else
  info "Cloning Statement Software into $INSTALL_DIR"
  git clone --branch "$BRANCH" "$REPO_URL" "$INSTALL_DIR"
fi

chmod +x "$INSTALL_DIR/setup.sh" "$INSTALL_DIR/scripts/statementsw"
ln -sfn "$INSTALL_DIR/scripts/statementsw" "$BIN_DIR/statementsw"
ok "Installed statementsw at $BIN_DIR/statementsw"

case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *)
    printf '[WARN] %s is not in PATH for this shell.\n' "$BIN_DIR"
    printf '[WARN] Run: export PATH="$HOME/.local/bin:$PATH"\n'
    ;;
esac

if [[ "$SKIP_SETUP" == "1" ]]; then
  ok "Install complete. Run: statementsw setup"
  exit 0
fi

"$BIN_DIR/statementsw" setup "${SETUP_ARGS[@]}"
