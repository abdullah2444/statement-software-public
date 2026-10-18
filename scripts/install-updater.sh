#!/usr/bin/env bash
# One-time setup of the Statement Software updater on a Docker server.
#
# Run as root on the server where the app container runs:
#   sudo bash scripts/install-updater.sh
# or straight from GitHub:
#   curl -fsSL https://raw.githubusercontent.com/abdullah2444/statement-software-public/main/scripts/install-updater.sh | sudo bash
#
# It installs a small background service (statement-updater) that installs
# releases when an admin clicks Update Now in Settings → Software Updates.
# It does not restart the app or change any data. The app gets the updater's
# folder the next time it is restarted, which the first update does for you:
#   sudo statement-updater install v5.1.0
set -Eeuo pipefail

CONTAINER="statement-software-v5"
REPOSITORY="abdullah2444/statement-software-public"
REF="main"
CHANNEL=""
PREFIX="/opt/statement-updater"
ENV_FILE="/etc/statement-updater.env"
UNIT_FILE="/etc/systemd/system/statement-updater.service"

usage() {
  cat <<'HELP'
Usage: install-updater.sh [options]

  --container NAME    App container name. Default: statement-software-v5
  --channel DIR       Folder shared with the app. Default: an "updater" folder
                      next to the app's database on this server
  --repository O/R    GitHub repository with the releases.
                      Default: abdullah2444/statement-software-public
  --ref REF           Branch or tag to take the updater from when this script
                      is run on its own (curl | bash). Default: main
HELP
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --container) CONTAINER="$2"; shift 2 ;;
    --channel) CHANNEL="$2"; shift 2 ;;
    --repository) REPOSITORY="$2"; shift 2 ;;
    --ref) REF="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage; exit 2 ;;
  esac
done

say() { printf '\n==> %s\n' "$*"; }
fail() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || fail "Run this as root (sudo)."
command -v docker >/dev/null || fail "Docker is not installed."
command -v python3 >/dev/null || fail "python3 is not installed. Install it with: apt install python3"
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' || fail "python3 is too old (3.8 or newer is needed)."
docker inspect "$CONTAINER" >/dev/null 2>&1 || fail "No container named $CONTAINER. Use --container NAME."

inspect() { docker inspect -f "$1" "$CONTAINER"; }
PROJECT="$(inspect '{{ index .Config.Labels "com.docker.compose.project" }}')"
WORKDIR="$(inspect '{{ index .Config.Labels "com.docker.compose.project.working_dir" }}')"
SERVICE="$(inspect '{{ index .Config.Labels "com.docker.compose.service" }}')"
[[ -n "$PROJECT" && "$PROJECT" != "<no value>" ]] || fail "$CONTAINER was not started with Docker Compose, which the updater needs."

# Where does /data/updater (inside the container) live on this server?
MOUNTED_CHANNEL="$(docker inspect "$CONTAINER" | python3 -c '
import json, sys
mounts = json.load(sys.stdin)[0].get("Mounts") or []
best = None
for m in mounts:
    dest = m.get("Destination", "").rstrip("/")
    if "/data/updater" == dest or "/data/updater".startswith(dest + "/"):
        if best is None or len(dest) > len(best[0]):
            best = (dest, m.get("Source", ""))
print(best[1] + "/data/updater"[len(best[0]):] if best else "")
')"
DB_HOST_PATH="$(docker inspect "$CONTAINER" | python3 -c '
import json, sys
info = json.load(sys.stdin)[0]
env = dict(e.split("=", 1) for e in info["Config"].get("Env") or [] if "=" in e)
db = env.get("DATABASE_PATH", "/data/firefly_statement.db")
for m in info.get("Mounts") or []:
    dest = m.get("Destination", "").rstrip("/")
    if db == dest or db.startswith(dest + "/"):
        print(m["Source"] + db[len(dest):]); break
')"

EXTRA_COMPOSE=""
if [[ -n "$MOUNTED_CHANNEL" ]]; then
  [[ -z "$CHANNEL" || "$CHANNEL" == "$MOUNTED_CHANNEL" ]] || fail "The app already uses $MOUNTED_CHANNEL as its updater folder."
  CHANNEL="$MOUNTED_CHANNEL"
else
  if [[ -z "$CHANNEL" ]]; then
    [[ -n "$DB_HOST_PATH" ]] || fail "Could not find the app's data folder. Use --channel DIR."
    CHANNEL="$(dirname "$DB_HOST_PATH")/updater"
  fi
  [[ -n "$WORKDIR" && -d "$WORKDIR" ]] || fail "Could not find the folder with the app's compose file."
  EXTRA_COMPOSE="$WORKDIR/docker-compose.updater.yml"
  say "Adding the updater folder to the app's Docker Compose setup ($EXTRA_COMPOSE)"
  cat > "$EXTRA_COMPOSE" <<YAML
# Added by install-updater.sh. Shares the updater's folder with the app, and
# names the installed version's image "current" (the updater sets which one).
services:
  $SERVICE:
    image: statement-software:current
    # Keep memory for the rest of the server even if a big backup or export runs.
    mem_limit: 768m
    volumes:
      - $CHANNEL:/data/updater
YAML
fi

say "Installing the updater into $PREFIX"
mkdir -p "$PREFIX/work" "$CHANNEL"
chmod 700 "$PREFIX"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || true)"
if [[ -n "$SCRIPT_DIR" && -f "$SCRIPT_DIR/../updater/statement_updater.py" ]]; then
  install -m 0755 "$SCRIPT_DIR/../updater/statement_updater.py" "$PREFIX/statement_updater.py"
  install -m 0644 "$SCRIPT_DIR/../updater/statement-updater.service" "$UNIT_FILE"
else
  RAW="https://raw.githubusercontent.com/$REPOSITORY/$REF/updater"
  curl -fsSL "$RAW/statement_updater.py" -o "$PREFIX/statement_updater.py"
  curl -fsSL "$RAW/statement-updater.service" -o "$UNIT_FILE"
  chmod 0755 "$PREFIX/statement_updater.py"
fi
python3 -m py_compile "$PREFIX/statement_updater.py"

cat > "$ENV_FILE" <<ENV
# Statement Software updater settings. Written by install-updater.sh.
STATEMENT_CONTAINER=$CONTAINER
STATEMENT_REPOSITORY=$REPOSITORY
STATEMENT_UPDATER_CHANNEL=$CHANNEL
STATEMENT_UPDATER_WORK=$PREFIX/work
STATEMENT_EXTRA_COMPOSE_FILES=$EXTRA_COMPOSE
ENV
chmod 600 "$ENV_FILE"

cat > /usr/local/bin/statement-updater <<'WRAPPER'
#!/usr/bin/env bash
# Run the Statement Software updater by hand, e.g. `statement-updater install v5.1.0`.
set -a; . /etc/statement-updater.env; set +a
exec python3 /opt/statement-updater/statement_updater.py "$@"
WRAPPER
chmod 0755 /usr/local/bin/statement-updater

say "Starting the statement-updater service"
systemctl daemon-reload
systemctl enable --now statement-updater.service
sleep 2
systemctl is-active --quiet statement-updater.service || fail "The service did not start. See: journalctl -u statement-updater"

say "Done"
cat <<DONE
The updater is running. Container: $CONTAINER. Shared folder: $CHANNEL
DONE
if [[ -n "$EXTRA_COMPOSE" ]]; then
  cat <<DONE

The app can't see the updater until it restarts with the new folder. Do that
with the first update, which makes a recovery backup before restarting:
  sudo statement-updater install v5.1.0
DONE
fi
