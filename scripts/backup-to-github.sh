#!/usr/bin/env bash
set -Eeuo pipefail

# GitHub Auto-Backup Script for Statement Software
# Commits database snapshots to a GitHub repository every 30 minutes

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

# Load configuration
if [[ -f "$PROJECT_DIR/.env" ]]; then
  set -a
  source "$PROJECT_DIR/.env"
  set +a
fi

# Configuration
BACKUP_REPO="${GITHUB_BACKUP_REPO:-}"
BACKUP_TOKEN="${GITHUB_BACKUP_TOKEN:-}"
BACKUP_DIR="${BACKUP_DIR:-$PROJECT_DIR/data/backups}"
DB_PATH="${DATABASE_PATH:-$PROJECT_DIR/data/firefly_statement.db}"
BACKUP_CLONE_DIR="${HOME}/.statement-software-backups"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

log_info() { echo -e "${GREEN}[INFO]${NC} $*"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $*"; }
log_error() { echo -e "${RED}[ERROR]${NC} $*" >&2; }
log_ok() { echo -e "${GREEN}[OK]${NC} $*"; }

check_config() {
  if [[ -z "$BACKUP_REPO" ]]; then
    log_error "GITHUB_BACKUP_REPO is not set"
    log_error "Add to .env: GITHUB_BACKUP_REPO=username/repo-name"
    log_error "Example: GITHUB_BACKUP_REPO=abdullah2444/statement-backups"
    exit 1
  fi

  if [[ -z "$BACKUP_TOKEN" ]]; then
    log_error "GITHUB_BACKUP_TOKEN is not set"
    log_error "Add to .env: GITHUB_BACKUP_TOKEN=ghp_xxxxx"
    log_error "Create token at: https://github.com/settings/tokens/new"
    log_error "Required scopes: repo (Full control of private repositories)"
    exit 1
  fi

  if [[ ! -f "$DB_PATH" ]]; then
    log_error "Database not found at: $DB_PATH"
    exit 1
  fi
}

git_authenticated() {
  GITHUB_BACKUP_TOKEN="$BACKUP_TOKEN" \
    GIT_ASKPASS="$SCRIPT_DIR/github-backup-askpass.sh" \
    GIT_TERMINAL_PROMPT=0 git -c credential.helper= "$@"
}

setup_backup_repo() {
  local repo_url="https://github.com/${BACKUP_REPO}.git"
  
  if [[ -d "$BACKUP_CLONE_DIR/.git" ]]; then
    log_info "Backup repository already cloned, updating..."
    cd "$BACKUP_CLONE_DIR"
    # Remove legacy credentials from both fetch and push URLs before networking.
    git config --replace-all remote.origin.url "$repo_url"
    git config --unset-all remote.origin.pushurl || [[ $? == 5 ]]
    git_authenticated pull --ff-only origin main 2>/dev/null || true
  else
    log_info "Cloning backup repository..."
    rm -rf "$BACKUP_CLONE_DIR"
    git_authenticated clone "$repo_url" "$BACKUP_CLONE_DIR" || {
      log_error "Failed to clone repository. Make sure $BACKUP_REPO exists on GitHub."
      log_error "Create it at: https://github.com/new"
      exit 1
    }
    cd "$BACKUP_CLONE_DIR"
  fi

  # Configure git
  git config user.name "Statement Software Backup Bot"
  git config user.email "backup@statement-software.local"
}

create_backup() {
  local timestamp=$(date +%Y%m%d-%H%M%S)
  local db_backup="$BACKUP_CLONE_DIR/database/firefly_statement_${timestamp}.db"
  local db_latest="$BACKUP_CLONE_DIR/database/firefly_statement_latest.db"
  
  mkdir -p "$BACKUP_CLONE_DIR/database"
  
  # Copy database with timestamp
  log_info "Creating database snapshot..."
  cp "$DB_PATH" "$db_backup"
  
  # Also maintain a "latest" copy for easy access
  cp "$DB_PATH" "$db_latest"
  
  # Get database stats
  local db_size=$(du -h "$db_backup" | cut -f1)
  local db_rows=""
  if command -v sqlite3 >/dev/null 2>&1; then
    db_rows=$(sqlite3 "$db_backup" "SELECT COUNT(*) FROM entries;" 2>/dev/null || echo "unknown")
  fi
  
  log_ok "Backup created: $db_size"
  [[ -n "$db_rows" && "$db_rows" != "unknown" ]] && log_info "Total entries: $db_rows"
  
  echo "$timestamp" > "$BACKUP_CLONE_DIR/LAST_BACKUP"
}

commit_and_push() {
  cd "$BACKUP_CLONE_DIR"
  
  # Add all database files
  git add database/ LAST_BACKUP
  
  # Check if there are changes
  if git diff --cached --quiet; then
    log_info "No changes to commit (database unchanged since last backup)"
    return 0
  fi
  
  # Commit with timestamp
  local timestamp=$(date +"%Y-%m-%d %H:%M:%S %Z")
  git commit -m "Automated backup: $timestamp" \
    -m "Database size: $(du -h database/firefly_statement_latest.db | cut -f1)"
  
  # Push to GitHub
  log_info "Pushing to GitHub..."
  git_authenticated push origin main || {
    log_error "Failed to push to GitHub"
    exit 1
  }
  
  log_ok "Backup pushed to GitHub: ${BACKUP_REPO}"
}

cleanup_old_backups() {
  cd "$BACKUP_CLONE_DIR/database"
  
  # Keep only the last 336 half-hourly backups (1 week)
  local backup_count=$(ls -1 firefly_statement_*.db 2>/dev/null | grep -v latest | wc -l)
  
  if [[ $backup_count -gt 336 ]]; then
    log_info "Cleaning up old backups (keeping last 336)..."
    ls -1t firefly_statement_*.db | grep -v latest | tail -n +337 | xargs rm -f
    
    cd "$BACKUP_CLONE_DIR"
    git add database/
    git commit -m "Cleanup: Removed old backups (keeping last 336)" || true
    git_authenticated push origin main || true
  fi
}

# Main execution
main() {
  log_info "Statement Software - GitHub Backup"
  log_info "Repository: $BACKUP_REPO"
  
  check_config
  setup_backup_repo
  create_backup
  commit_and_push
  cleanup_old_backups
  
  log_ok "Backup complete!"
  log_info "View backups at: https://github.com/$BACKUP_REPO"
}

main "$@"
