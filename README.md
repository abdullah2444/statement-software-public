# Statement Software

Open-source statement management software with Docker Compose/Python setup, full backups, restore, and a friendly `statementsw` terminal command.

Git hosting stores code only. Your `.env`, database, uploads, backups, logs, and business data stay private on your machine or server.

## China-Friendly Install

Gitee is the primary mirror for China-friendly access:

- Gitee: https://gitee.com/abdullah24/statement-software-public
- GitHub mirror: https://github.com/abdullah2444/statement-software-public

Clone from Gitee, then run setup:

```bash
git clone https://gitee.com/abdullah24/statement-software-public.git
cd statement-software-public
bash setup.sh quickstart
```

If the Gitee repo is private, clone with SSH instead:

```bash
git clone git@gitee.com:abdullah24/statement-software-public.git
cd statement-software-public
bash setup.sh quickstart
```

After setup, open:

```text
http://SERVER_IP:18451
```

## Docker Compose Install

This project includes a ready `docker-compose.yml`. To install manually with Compose:

```bash
git clone https://gitee.com/abdullah24/statement-software-public.git
cd statement-software-public
cp .env.example .env
```

Edit `.env` and set a private `SECRET_KEY`, then start:

```bash
docker compose up -d --build
docker compose ps
```

Stop or update later:

```bash
docker compose down
git pull
docker compose up -d --build
```

## Paste-Only Compose Install

If you do not want to clone the repo first, paste this into `compose.yml` on the server:

```yaml
services:
  statement-software:
    build:
      context: https://gitee.com/abdullah24/statement-software-public.git#main
      dockerfile: Dockerfile
    image: statement-software:latest
    container_name: statement-software
    ports:
      - "18451:18451"
    volumes:
      - ./statement-software-data:/data
    environment:
      PORT: "18451"
      HOST: "0.0.0.0"
      FLASK_DEBUG: "0"
      DATABASE_PATH: /data/statement_software.db
      UPLOAD_DIR: /data/uploads
      BACKUP_DIR: /data/backups
      MAX_UPLOAD_MB: "512"
      APP_NAME: "Statement Software"
      BRAND_NAME: "Statement"
      COMPANY_NAME: "Your Company"
      DEFAULT_PROFIT_EXPENSE_ACCOUNT_NAME: "Company Profit"
      SESSION_COOKIE_SECURE: "0"
    restart: unless-stopped
```

Then run:

```bash
docker compose up -d --build
```

This downloads the source from Gitee during the build. Private app data is stored in `./statement-software-data`, not in Git.

To change config, create a `.env` file in the same folder as `compose.yml`:

```bash
cat > .env <<'EOF'
PORT=18451
HOST=0.0.0.0
APP_NAME="Statement Software"
BRAND_NAME="Statement"
COMPANY_NAME="Your Company"
MAX_UPLOAD_MB=512
SESSION_COOKIE_SECURE=0
OPENROUTER_API_KEY=
FX_PROXY_URL=
EOF
```

Then restart the stack:

```bash
docker compose up -d --build
```

Fresh installs create this default login:

```env
INITIAL_ADMIN_USERNAME=admin
INITIAL_ADMIN_PASSWORD=admin123
INITIAL_ADMIN_MUST_CHANGE=1
```

Log in with `admin / admin123`. The app will force a password change immediately after login.

Those admin values are only used when the database has no users yet. If you already started an old install and cannot log in, check the generated bootstrap file:

```bash
docker compose exec statement-software cat /data/admin_bootstrap.txt
```

If there is no important data yet, you can recreate the database and get the new default login:

```bash
docker compose down
rm -rf statement-software-data
docker compose up -d --build
```

Important: run Compose commands on the server/host, in the folder where `compose.yml` exists. Do not run `bash setup.sh quickstart` inside the container. The runtime container only contains the app files:

```text
/app/app.py
/app/templates
/app/static
```

If you are at a prompt like `root@statement-software:/app#`, you are already inside the container. Type:

```bash
exit
```

Then manage the app from the server folder that contains `compose.yml`:

```bash
docker compose ps
docker compose logs -f statement-software
docker compose down
docker compose up -d --build
```

To update a paste-only Compose install to the latest Gitee code:

```bash
docker compose down
docker compose build --no-cache statement-software
docker compose up -d
```

## Optional One-Command Installer

If GitHub raw access is available, the installer can still be used:

```bash
curl -fsSL https://raw.githubusercontent.com/abdullah2444/statement-software-public/main/scripts/install.sh | bash
```

Custom install examples:

```bash
curl -fsSL https://raw.githubusercontent.com/abdullah2444/statement-software-public/main/scripts/install.sh | bash -s -- --port 8080
curl -fsSL https://raw.githubusercontent.com/abdullah2444/statement-software-public/main/scripts/install.sh | bash -s -- --method python --dir ~/statement-software
```

After install, use:

```bash
statementsw setup
statementsw start
statementsw status
statementsw doctor
statementsw backup
statementsw restore ./statement-full-backup-YYYYMMDD-HHMMSS.tar.gz
```

## Useful Commands

```bash
statementsw help
statementsw setup
statementsw start
statementsw stop
statementsw restart
statementsw status
statementsw doctor
statementsw backup
statementsw export
statementsw restore ./data/backups/statement-full-backup-YYYYMMDD-HHMMSS.tar.gz
statementsw reset-admin-password --admin-user admin
statementsw update
```

The classic project-local command still works:

```bash
bash setup.sh quickstart
```

## Configuration

`setup.sh` creates `.env` for you. `.env` is private and ignored by Git.

Important settings:

- `PORT`: browser port.
- `HOST`: bind address.
- `DATA_DIR`: private runtime data folder.
- `DATABASE_PATH`: SQLite database path.
- `UPLOAD_DIR`: uploaded image/file path.
- `BACKUP_DIR`: backup path.
- `MAX_UPLOAD_MB`: max Settings restore upload size. Default: `512`.
- `APP_NAME`: visible app name. Default: `Statement Software`.
- `BRAND_NAME`: short UI brand. Default: `Statement`.
- `COMPANY_NAME`: company name shown on PDFs. Default: `Your Company`.
- `DEFAULT_PROFIT_EXPENSE_ACCOUNT_NAME`: default account for company profit mirroring.
- `SEED_DEMO_DATA`: keep `0` for normal fresh installs.
- `SOURCE_CSV_PATH`: optional path to demo CSV when `SEED_DEMO_DATA=1`.
- `DEMO_CLIENT_NAME`: client name used only when importing demo CSV data.
- `SECRET_KEY`: private Flask session secret.
- `SESSION_COOKIE_SECURE`: use `1` only when serving through HTTPS.
- `OPENROUTER_API_KEY`: optional AI image parsing key.
- `FX_PROXY_URL`: optional proxy URL for exchange-rate API calls.
- `INITIAL_ADMIN_USERNAME`: first-run admin username. Default: `admin`.
- `INITIAL_ADMIN_PASSWORD`: first-run admin password. Default: `admin123`.
- `INITIAL_ADMIN_MUST_CHANGE`: set to `1` to force first-run admin to change password after login. Default: `1`.

## API

The app includes a JSON API for integrations and future mobile apps. It supports normal username/password login for people and API tokens for tools.

- Beginner guide: `docs/API.md`
- OpenAPI file: `docs/openapi.yaml`

API token access levels are simple: `full_control`, `read_only`, and `client_portal`.

## Compose File Format

The checked-in files are YAML. Use:

- `docker-compose.yml` after cloning the repo.
- `docker-compose.remote.yml` as the paste-only version that downloads the repo from Gitee.
- `compose.env.example` as a template for the `.env` config file used by the paste-only version.

On a server, either file can be renamed to `compose.yml`. The default browser port is `18451`.

## Backups

Create a full backup:

```bash
statementsw backup
```

The full backup package contains:

- `manifest.json`
- `statement_software.db`
- `uploads/`

Restore a full backup:

```bash
statementsw restore ./data/backups/statement-full-backup-YYYYMMDD-HHMMSS.tar.gz
```

Legacy raw SQLite exports are still accepted, but they are database-only and do not include uploaded images.

## Repository Safety

Do not commit:

- `.env`
- `data/`
- databases
- uploads
- backups
- logs
- archives
- spreadsheets or real business files
- duplicate extracted app folders

## Updating

```bash
statementsw backup
statementsw update
statementsw doctor
```
