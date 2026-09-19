#!/usr/bin/env bash
# Git reads credentials from the process environment, never from the remote URL.
set -eu
case "${1:-}" in
  *Username*) printf '%s\n' 'x-access-token' ;;
  *Password*) printf '%s\n' "${GITHUB_BACKUP_TOKEN:?Backup token is not set}" ;;
  *) exit 1 ;;
esac
