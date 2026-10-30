# Software updates

Admins update the app from **Settings → Software Updates**. When a new
release is published on GitHub, admins see "Version X is available" at the top
of every page. The card in Settings shows the release notes and an
**Update Now** button. The app then:

1. **Preparing**: checks the release on GitHub, checks there is enough disk
   space, downloads it and builds it. People keep working normally.
2. **Backing up**: pauses saving (pages still open, but Save buttons get "The
   software is being updated" for a few minutes), waits for saves already in
   progress, then makes a recovery backup and checks it can be opened. If
   cloud backup is set up, a cloud backup is made too. If the recovery backup
   fails, the update stops here and nothing is changed. If only the cloud
   backup fails (for example Backblaze is having trouble), the update goes on
   with the recovery backup, and Settings → Cloud Backup shows the reason; the
   next scheduled cloud backup catches up.
3. **Installing**: restarts the app on the new version. The page shows
   "Reconnecting…" and comes back by itself.
4. **Checking**: checks that the new version starts, reports the right
   version number, and that its database passes an integrity check.
5. **Complete**: saving is turned back on.

If installing or checking fails, the updater stops the new version, puts the
database from the recovery backup back, starts the previous version, checks
it, and only then turns saving back on. The card shows "Put back" in the
update history with the reason. If even that fails, saving **stays paused**
so nothing can be saved into a half-updated app, and the history says
"Needs attention" with the name of the recovery backup.

The recovery backup is an ordinary full backup in the server's backup folder
(named `statement-full-backup-…-before-update-to-X.tar.gz`), so it also shows
up in Settings → Backup & Restore.

## How it fits together

```
 Browser ──▶ App container (Flask)          Updater service (on the server, not in Docker)
             Settings → Software Updates     /opt/statement-updater/statement_updater.py
             writes request.json      ──▶   reads request.json
             reads status.json        ◀──   writes status.json, history.json, heartbeat.json
             holds saves while        ◀──   writes pause.json
             pause.json exists, then  ──▶   waits for pause-ack.json
             writes pause-ack.json
                                             docker build / docker compose up / docker exec
```

The two talk only through files in one shared folder: `/data/updater` inside
the container, and an `updater` folder next to the database on the server.
The app can only *ask* for a release that GitHub lists as published. The
updater checks with GitHub itself again, and refuses drafts, test releases
("pre-release"), unknown names and older versions. Only signed-in admins can
ask, and the request goes through the app's normal anti-forgery check.

The updater keeps running while the app restarts because it is a separate
service on the server (systemd), not part of the app.

## One-time setup on a Docker server

This is done once per server. After it, updates are installed from Settings.

### 1. Make sure the server's code matches GitHub

The updater installs exactly what is in a GitHub release. Anything that only
exists in the server's own copy of the code (for example extra CSS or
template changes) would disappear in the first update. So first compare the
server's code folder with GitHub, and put any changes worth keeping into the
repository and a release. To see what differs, on the server:

```
cd /path/to/statement-software-public      # the folder with docker-compose.yml
git status --short
git diff
```

### 2. Check the app's settings are in the .env file

The updater restarts the app with `docker compose`, which reads settings
such as `SECRET_KEY` from the `.env` file next to `docker-compose.yml`. If the
app was started with settings typed on the command line instead, they would
change on restart. The updater checks this before it changes anything and
names any setting that would change (never its value).

### 3. Install the updater service

On the server, as root:

```
curl -fsSL https://raw.githubusercontent.com/abdullah2444/statement-software-public/main/scripts/install-updater.sh | sudo bash
```

or, from a copy of the repository: `sudo bash scripts/install-updater.sh`.
It finds the app container (default `statement-software-v5`; use
`--container NAME` for another), installs the `statement-updater` service and
the `statement-updater` command, and, if the app's data folder isn't shared
yet, adds a small `docker-compose.updater.yml` next to the compose file. It
does not restart the app or touch any data.

### 4. Install the first release from the command line

```
sudo statement-updater install v5.1.0
```

This runs the same steps as Update Now. Because the version running before
this has no updater support, it makes the recovery backup with the app
briefly stopped instead of pausing saving. From then on, use Settings.

### After setup

Don't rebuild the app by hand with `docker compose up --build`, `setup.sh` or
`statementsw update` from the old code folder: that would build whatever code
is in that folder, not the release that is installed. If you need to, first
check out the installed release there (`git fetch --tags && git checkout v5.1.0`).

## Package versions

`requirements.txt` lists the packages the app uses. `constraints.txt` pins
the exact version of every package, including the ones those pull in, so
every build gets the same code. The **Dependency security check** on GitHub
checks `constraints.txt` every Monday and emails the owner if a package gets
a known security problem.

To change a version: edit `requirements.txt`, then rebuild the lock file in a
clean Python 3.11 environment and run the check:

```
python -m venv /tmp/v && /tmp/v/bin/pip install -r requirements.txt
/tmp/v/bin/pip freeze --exclude pip --exclude setuptools --exclude wheel > constraints.txt
/tmp/v/bin/pip install pip-audit && /tmp/v/bin/pip-audit -r constraints.txt
```

## Publishing a release

1. Make the changes on a branch and merge them into `main` after the tests pass.
2. Raise the number in `VERSION` (for example `5.1.0` → `5.1.1` for fixes,
   `5.2.0` for new features) and add a section with that number to
   `CHANGELOG.md`, written for the people who use the app.
3. Tag it and push the tag:
   ```
   git tag v5.1.1 && git push origin v5.1.1
   ```
4. GitHub runs the tests, checks the tag matches `VERSION`, builds the Docker
   image, and creates a **draft** release with the notes from `CHANGELOG.md`.
5. Look over the draft on GitHub and press **Publish release**. Servers see it
   at their next check (every 6 hours, or straight away with Check for Updates).

Nothing reaches a server until step 5, and releases are never changed after
publishing: a fix is a new version number.

## Useful commands on the server

| Command | What it does |
|---|---|
| `systemctl status statement-updater` | Is the updater running? |
| `journalctl -u statement-updater -f` | Watch what it is doing |
| `sudo statement-updater status` | Show the last update's status |
| `sudo statement-updater install v5.1.1` | Install a release from the command line |
| `ls <data folder>/updater/logs/` | A detailed log of each update |

If the updater service stopped in the middle of an update (for example the
server rebooted), it finishes the job when it starts again: before the restart
it simply turns saving back on; after it, it puts the previous version back.
If saving is stuck paused and the updater is not running, the Software
Updates card shows a **Turn Saving Back On** button.

## Settings

The updater reads `/etc/statement-updater.env`:

| Setting | Default |
|---|---|
| `STATEMENT_CONTAINER` | `statement-software-v5` |
| `STATEMENT_REPOSITORY` | `abdullah2444/statement-software-public` |
| `STATEMENT_UPDATER_CHANNEL` | the shared folder (set by the installer) |
| `STATEMENT_UPDATER_WORK` | `/opt/statement-updater/work` (downloads and builds) |
| `STATEMENT_HEALTH_TIMEOUT` | `180` seconds for the new version to pass its checks |
| `STATEMENT_BUILD_ARGS` | extra `docker build` options, normally empty |

The app reads `UPDATE_REPOSITORY` (same default) and `UPDATER_DIR`
(default `/data/updater`).

## Limits

- Anything saved between the recovery backup and a rollback is lost by the
  rollback. Saving is paused for exactly that window, so in practice only
  logins and automatic background records are affected.
- Release downloads come from GitHub, and the Docker build downloads Python
  packages, so the server needs internet access during an update.
- The updater updates the app, not itself. If a release changes the updater,
  its notes will say to run `install-updater.sh` again.
