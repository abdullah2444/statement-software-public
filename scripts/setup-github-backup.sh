#!/usr/bin/env bash
set -Eeuo pipefail

# Setup script for GitHub 30-minute backups

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
ENV_FILE="$PROJECT_DIR/.env"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

log_info() { echo -e "${GREEN}[INFO]${NC} $*"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $*"; }
log_error() { echo -e "${RED}[ERROR]${NC} $*" >&2; }
log_ok() { echo -e "${GREEN}[OK]${NC} $*"; }

print_header() {
  echo ""
  echo "╔════════════════════════════════════════════════════════════╗"
  echo "║   Statement Software - GitHub Backup Setup                ║"
  echo "╚════════════════════════════════════════════════════════════╝"
  echo ""
}

prompt() {
  local var_name="$1"
  local prompt_text="$2"
  local default="${3:-}"
  
  if [[ -n "$default" ]]; then
    read -p "$prompt_text [$default]: " value
    value="${value:-$default}"
  else
    read -p "$prompt_text: " value
  fi
  
  eval "$var_name='$value'"
}

if [[ "${1:-}" == "--schedule-only" ]]; then
  mkdir -p "$PROJECT_DIR/data"
  existing_cron="$(crontab -l 2>/dev/null || true)"
  {
    printf '%s\n' "$existing_cron" | sed '\|backup-to-github.sh|d'
    printf '*/30 * * * * cd "%s" && bash scripts/backup-to-github.sh >> data/backup-github.log 2>&1\n' "$PROJECT_DIR"
  } | crontab -
  log_ok "Backup schedule updated: every 30 minutes"
  exit 0
fi

print_header

log_info "This wizard will help you set up 30-minute GitHub backups for your database."
echo ""

# Check if already configured
if grep -q "GITHUB_BACKUP_REPO=" "$ENV_FILE" 2>/dev/null; then
  log_warn "GitHub backup appears to be already configured in .env"
  read -p "Do you want to reconfigure? [y/N]: " reconfigure
  if [[ ! "$reconfigure" =~ ^[Yy] ]]; then
    log_info "Exiting. To test your backup manually, run:"
    log_info "  bash scripts/backup-to-github.sh"
    exit 0
  fi
fi

echo ""
log_info "Step 1: Create a GitHub repository for backups"
echo "────────────────────────────────────────────────"
echo "1. Go to: https://github.com/new"
echo "2. Create a PRIVATE repository (e.g., 'statement-backups')"
echo "3. Initialize with a README (optional)"
echo ""
read -p "Press Enter when you've created the repository..."

echo ""
prompt BACKUP_REPO "Enter repository name (format: username/repo-name)" ""

while [[ ! "$BACKUP_REPO" =~ ^[a-zA-Z0-9_-]+/[a-zA-Z0-9_-]+$ ]]; do
  log_error "Invalid format. Use: username/repository-name"
  prompt BACKUP_REPO "Enter repository name (format: username/repo-name)" ""
done

echo ""
log_info "Step 2: Create a GitHub Personal Access Token"
echo "────────────────────────────────────────────────"
echo "1. Go to: https://github.com/settings/tokens/new"
echo "2. Name: 'Statement Software Backups'"
echo "3. Expiration: No expiration (or custom)"
echo "4. Scopes: Check 'repo' (Full control of private repositories)"
echo "5. Click 'Generate token'"
echo "6. COPY THE TOKEN (you won't see it again!)"
echo ""
read -p "Press Enter when you have the token..."

echo ""
prompt BACKUP_TOKEN "Paste your GitHub token (ghp_...)" ""

while [[ ! "$BACKUP_TOKEN" =~ ^(ghp_|github_pat_)[a-zA-Z0-9_]+ ]]; do
  log_error "Invalid token format. Should start with 'ghp_' or 'github_pat_'"
  prompt BACKUP_TOKEN "Paste your GitHub token" ""
done

# Update .env file
log_info "Updating .env file..."
echo "" >> "$ENV_FILE"
echo "# GitHub Hourly Backup Configuration" >> "$ENV_FILE"
echo "GITHUB_BACKUP_REPO=$BACKUP_REPO" >> "$ENV_FILE"
echo "GITHUB_BACKUP_TOKEN=$BACKUP_TOKEN" >> "$ENV_FILE"

log_ok "Configuration saved to .env"

# Test the backup
echo ""
log_info "Step 3: Testing backup..."
echo "────────────────────────────────────────────────"
if bash "$SCRIPT_DIR/backup-to-github.sh"; then
  log_ok "Test backup successful!"
else
  log_error "Test backup failed. Check the error above."
  exit 1
fi

# Setup cron job
echo ""
log_info "Step 4: Setting up 30-minute cron job"
echo "────────────────────────────────────────────────"

CRON_CMD="*/30 * * * * cd $PROJECT_DIR && bash scripts/backup-to-github.sh >> data/backup-github.log 2>&1"

# Check if cron job already exists
if crontab -l 2>/dev/null | grep -F "backup-to-github.sh" >/dev/null; then
  log_warn "Cron job already exists"
  read -p "Replace existing cron job? [y/N]: " replace
  if [[ "$replace" =~ ^[Yy] ]]; then
    # Remove old cron job
    crontab -l 2>/dev/null | grep -v "backup-to-github.sh" | crontab -
    log_info "Old cron job removed"
  else
    log_info "Keeping existing cron job"
    CRON_CMD=""
  fi
fi

if [[ -n "$CRON_CMD" ]]; then
  # Add new cron job
  (crontab -l 2>/dev/null; echo "$CRON_CMD") | crontab -
  log_ok "Cron job added (runs every 30 minutes)"
fi

# Summary
echo ""
echo "╔════════════════════════════════════════════════════════════╗"
echo "║   ✅ GitHub Backup Setup Complete!                         ║"
echo "╚════════════════════════════════════════════════════════════╝"
echo ""
log_info "Backup repository: https://github.com/$BACKUP_REPO"
log_info "Schedule: Every 30 minutes (at minutes 0 and 30)"
log_info "Retention: Last 336 snapshots in the working tree (about 1 week)"
echo ""
log_info "Useful commands:"
echo "  • Manual backup:  bash scripts/backup-to-github.sh"
echo "  • View cron jobs: crontab -l | grep backup"
echo "  • View logs:      tail -f data/backup-github.log"
echo "  • Disable backup: crontab -e  (then comment out the line)"
echo ""
log_ok "Your database will be backed up to GitHub every 30 minutes!"
