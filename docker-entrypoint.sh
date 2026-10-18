#!/bin/sh
# Start the app as the unprivileged "app" user, not root.
#
# The data folder is mounted from the server, where older versions (which ran
# as root) left everything owned by root. Give it to the app user first, then
# drop root for good.
set -e

if [ "$(id -u)" = "0" ]; then
    for path in /data "${DATABASE_PATH:-}" "${UPLOAD_DIR:-}" "${BACKUP_DIR:-}"; do
        [ -n "$path" ] && [ -e "$path" ] && chown -R app:app "$path"
    done
    # SQLite keeps two side files next to the database; they must be the app's too.
    for side in "${DATABASE_PATH:-/data/firefly_statement.db}-wal" "${DATABASE_PATH:-/data/firefly_statement.db}-shm"; do
        [ -e "$side" ] && chown app:app "$side"
    done
    exec setpriv --reuid=app --regid=app --init-groups "$@"
fi

exec "$@"
